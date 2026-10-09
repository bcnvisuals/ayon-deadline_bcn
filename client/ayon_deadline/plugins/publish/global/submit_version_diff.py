# -*- coding: utf-8 -*-
"""Submit a 'version diff' render of a published render to Deadline."""
import os
import json
import getpass

import pyblish.api

from ayon_core.pipeline import publish
from ayon_core.pipeline.workfile import get_workdir

from ayon_deadline import version_diff
from ayon_deadline.lib import (
    JobType,
    DeadlineJobInfo,
    get_instance_job_envs,
)

# Environment passed from the publishing process to the diff jobs
ENV_KEYS = [
    "AYON_APP_NAME",
    "AYON_HOST_NAME",
    "AYON_USERNAME",
    "AYON_BUNDLE_NAME",
    "AYON_STUDIO_BUNDLE_NAME",
    "AYON_USE_STAGING",
    "AYON_IN_TESTS",
    "AYON_LOG_NO_COLORS",
    "FTRACK_API_KEY",
    "FTRACK_API_USER",
    "FTRACK_SERVER",
]


class SubmitVersionDiff(pyblish.api.InstancePlugin,
                        publish.AYONPyblishPluginMixin):
    """Render a diff against the previous version of the same product.

    Runs after the version is integrated (and synced to ftrack), so it never
    delays the regular publish. When an earlier version of the same product
    and task exists, Deadline jobs render a `<product>_diff` product that
    highlights what changed. If resolution or frame range differ, nothing
    is rendered and a note is added to the ftrack version instead.

    In farm publish jobs, eligibility is decided on families of the source
    instance (`sourceFamilies`, set by `ProcessSubmittedJobOnFarm`).
    """

    label = "Submit Version Diff to Deadline"
    order = pyblish.api.IntegratorOrder + 0.49
    families = ["render"]
    targets = ["local", "farm"]
    settings_category = "deadline"

    # Settings
    enabled = False
    profiles = []
    diff_suffix = "_diff"
    threshold = 6
    blur_size = 9
    min_changed_percent = 0.05
    max_output_width = 2048
    chunk_size = 20
    priority = 40
    group = ""
    pool = ""
    department = ""

    def process(self, instance):
        on_farm = os.getenv("AYON_PUBLISH_JOB") == "1"
        product_name = instance.data["productName"]
        if instance.data.get(version_diff.DIFF_FLAG_KEY):
            self.log.debug("Instance is a version diff result. Skipping.")
            return
        if not on_farm and instance.data.get("farm"):
            self.log.debug("Diff will be submitted by the farm publish job.")
            return
        if product_name.endswith(self.diff_suffix):
            return
        if not version_diff.is_beauty_aov(instance.data.get("aov")):
            self.log.debug(
                f"Skipping non-beauty AOV '{instance.data.get('aov')}'."
            )
            return
        if not self._is_enabled_by_profile(instance):
            return

        version_entity = instance.data.get("versionEntity")
        if not version_entity:
            self.log.debug("Instance was not integrated. Skipping.")
            return

        context = instance.context
        project_name = context.data["projectName"]
        anatomy = context.data["anatomy"]
        prev_version = version_diff.find_previous_version(
            project_name, version_entity
        )
        if not prev_version:
            self.log.info(
                f"No earlier version of '{product_name}' for this task."
            )
            return

        new_seq = version_diff.get_image_sequence(
            project_name, version_entity["id"], anatomy
        )
        if not new_seq:
            self.log.info("No image sequence published. Skipping diff.")
            return
        old_seq = version_diff.get_image_sequence(
            project_name, prev_version["id"], anatomy,
            preferred_name=new_seq["name"],
        )
        if not old_seq:
            self._report_skip(
                instance, prev_version,
                [f"v{prev_version['version']:03d} has no image sequence"]
            )
            return

        new_first = new_seq["frames"][min(new_seq["frames"])]
        old_first = old_seq["frames"][min(old_seq["frames"])]
        missing = [
            path for path in (new_first, old_first)
            if not os.path.exists(path)
        ]
        if missing:
            self._report_skip(
                instance, prev_version,
                ["files not found on disk: {}".format(", ".join(missing))]
            )
            return

        new_info = version_diff.get_image_info(new_first)
        old_info = version_diff.get_image_info(old_first)
        reasons = version_diff.compare_sequences(
            new_seq, old_seq, new_info, old_info
        )
        if reasons:
            self._report_skip(instance, prev_version, reasons)
            return

        self._submit(
            instance, version_entity, prev_version,
            new_seq, old_seq, new_info, old_info,
        )

    def _is_enabled_by_profile(self, instance):
        context = instance.context
        host_name = context.data["hostName"]
        product_base_type = (
            instance.data.get("productBaseType")
            or instance.data.get("productType")
        )
        families = set(
            instance.data.get("sourceFamilies")
            or instance.data.get("families")
            or []
        )
        families.add(instance.data.get("productType"))
        task_type = (
            (instance.data.get("anatomyData") or {}).get("task") or {}
        ).get("type")

        for profile in self.profiles:
            checks = (
                (profile["host_names"], host_name in profile["host_names"]),
                (
                    profile["product_base_types"],
                    product_base_type in profile["product_base_types"],
                ),
                (profile["task_types"], task_type in profile["task_types"]),
                (
                    profile["families"],
                    bool(families & set(profile["families"])),
                ),
            )
            if all(matched for values, matched in checks if values):
                return True

        self.log.debug(
            f"No version diff profile for host '{host_name}',"
            f" product base type '{product_base_type}',"
            f" families {sorted(f for f in families if f)}."
        )
        return False

    def _report_skip(self, instance, prev_version, reasons):
        text = (
            "Version diff against v{:03d} not rendered: {}.".format(
                prev_version["version"], "; ".join(reasons)
            )
        )
        self.log.warning(text)
        self._add_ftrack_note(instance, text)

    def _add_ftrack_note(self, instance, text):
        session = instance.context.data.get("ftrackSession")
        asset_versions_data = instance.data.get(
            "ftrackIntegratedAssetVersionsData"
        )
        if not session or not asset_versions_data:
            self.log.debug("No ftrack AssetVersion to add the note to.")
            return

        user = session.query(
            f"User where username is \"{session.api_user}\""
        ).first()
        for asset_version_data in asset_versions_data.values():
            asset_version_data["asset_version"].create_note(
                text, author=user
            )
        try:
            session.commit()
        except Exception:
            session.rollback()
            self.log.warning("Failed to add ftrack note.", exc_info=True)

    def _get_staging_dir(self, instance, diff_product_name, new_version):
        context = instance.context
        anatomy = context.data["anatomy"]
        workdir = get_workdir(
            context.data["projectEntity"],
            instance.data["folderEntity"],
            instance.data["taskEntity"],
            context.data["hostName"],
            anatomy=anatomy,
            project_settings=context.data["project_settings"],
        )
        return os.path.join(
            str(workdir),
            "renders",
            "version_diff",
            f"{diff_product_name}_v{new_version:03d}",
        )

    def _rootless(self, anatomy, path):
        success, rootless_path = anatomy.find_root_template_from_path(path)
        if not success:
            self.log.warning(f"Could not find root for '{path}'.")
            return path
        return rootless_path

    def _submit(
        self, instance, version_entity, prev_version,
        new_seq, old_seq, new_info, old_info,
    ):
        context = instance.context
        anatomy = context.data["anatomy"]
        product_name = instance.data["productName"]
        diff_product_name = f"{product_name}{self.diff_suffix}"
        new_version = version_entity["version"]
        old_version = prev_version["version"]
        frames = sorted(new_seq["frames"])

        staging_dir = self._get_staging_dir(
            instance, diff_product_name, new_version
        )
        os.makedirs(os.path.join(staging_dir, "stats"), exist_ok=True)
        staging_dir_rootless = self._rootless(anatomy, staging_dir)

        frame_padding = len(
            version_diff.FRAME_REGEX.search(
                os.path.basename(new_seq["frames"][frames[0]])
            ).group(1)
        )
        project_name = context.data["projectName"]
        file_basename = "{}_{}_v{:03d}".format(
            context.data["projectEntity"]["code"],
            diff_product_name,
            new_version,
        )
        ocio_config = (
            (new_seq["colorspaceData"].get("config") or {}).get("path")
        )
        if ocio_config and not os.path.exists(ocio_config):
            ocio_config = None

        spec = {
            "project_name": project_name,
            "folder_path": instance.data["folderPath"],
            "task_name": instance.data["task"],
            "product_name": product_name,
            "product_group": instance.data.get("productGroup"),
            "diff_product_name": diff_product_name,
            "new_version": new_version,
            "old_version": old_version,
            "new_version_id": version_entity["id"],
            "old_version_id": prev_version["id"],
            "frames": {
                str(frame): [
                    self._rootless(anatomy, new_seq["frames"][frame]),
                    self._rootless(anatomy, old_seq["frames"][frame]),
                ]
                for frame in frames
            },
            "frame_padding": frame_padding,
            "file_basename": file_basename,
            "width": new_info["width"],
            "height": new_info["height"],
            "fps": instance.data.get("fps") or context.data.get("fps"),
            "new_channels": version_diff.get_rgb_channel_indexes(
                new_info["channelnames"]
            ),
            "old_channels": version_diff.get_rgb_channel_indexes(
                old_info["channelnames"]
            ),
            "new_colorspace": new_seq["colorspaceData"].get("colorspace"),
            "old_colorspace": old_seq["colorspaceData"].get("colorspace"),
            "ocio_config": ocio_config,
            "settings": {
                "threshold": self.threshold,
                "blur_size": self.blur_size,
                "min_changed_percent": self.min_changed_percent,
                "max_output_width": self.max_output_width,
            },
            "staging_dir": staging_dir_rootless,
            "staging_dir_rootless": staging_dir_rootless,
            "publish_metadata_path": self._rootless(
                anatomy,
                os.path.join(
                    staging_dir, f"{diff_product_name}_metadata.json"
                ),
            ),
            "source": instance.data.get("source")
            or context.data.get("currentFile")
            or "",
            "user": context.data.get("user") or getpass.getuser(),
        }
        spec_path = os.path.join(staging_dir, "version_diff_spec.json")
        with open(spec_path, "w") as stream:
            json.dump(spec, stream, indent=1)
        spec_path_rootless = self._rootless(anatomy, spec_path)

        environment = self._get_environment(instance)
        if not environment.get("AYON_PROJECT_NAME"):
            self.log.warning("Missing AYON context env. Skipping diff.")
            return

        deadline_addon = context.data["ayonAddonsManager"]["deadline"]
        server_name = self._get_server_name(instance)
        batch_name = self._get_batch_name(instance, product_name, new_version)
        username = self._get_username(instance)
        title = "{} v{:03d} vs v{:03d}".format(
            product_name, new_version, old_version
        )

        def _job_info(name, job_type, dependencies=None, **kwargs):
            job_info = DeadlineJobInfo(
                Name=name,
                BatchName=batch_name,
                UserName=username,
                Priority=self.priority,
                Group=self.group or None,
                Pool=self.pool or None,
                Department=self.department or None,
                Comment=f"Version diff {title}",
                JobDependencies=dependencies or [],
                **kwargs
            )
            job_info.OutputDirectory.append(staging_dir)
            job_info.EnvironmentKeyValue.update(environment)
            job_info.EnvironmentKeyValue.update(job_type.get_job_env())
            return job_info

        render_job = deadline_addon.submit_ayon_plugin_job(
            server_name,
            [
                "--headless", "addon", "deadline", "version-diff-render",
                spec_path_rootless,
                "--start", "<STARTFRAME>",
                "--end", "<ENDFRAME>",
            ],
            _job_info(
                f"Version Diff - {title}",
                JobType.PUBLISH,
                Frames=version_diff.frames_to_deadline_str(frames),
                ChunkSize=self.chunk_size,
            ),
            single_frame_only=False,
        )
        render_job_id = render_job["response"]["_id"]

        finalize_job = deadline_addon.submit_ayon_plugin_job(
            server_name,
            [
                "--headless", "addon", "deadline", "version-diff-finalize",
                spec_path_rootless,
            ],
            _job_info(
                f"Version Diff Review - {title}",
                JobType.PUBLISH,
                dependencies=[render_job_id],
            ),
        )
        finalize_job_id = finalize_job["response"]["_id"]

        publish_job = deadline_addon.submit_ayon_plugin_job(
            server_name,
            [
                "--headless", "publish", spec["publish_metadata_path"],
                "--targets", "farm",
            ],
            _job_info(
                f"Publish - {diff_product_name}",
                JobType.PUBLISH,
                dependencies=[finalize_job_id],
            ),
        )
        self.log.info(
            "Submitted version diff {} -> jobs {}, {}, {}".format(
                title,
                render_job_id,
                finalize_job_id,
                publish_job["response"]["_id"],
            )
        )

    def _get_environment(self, instance):
        environment = {}
        if os.getenv("AYON_PUBLISH_JOB") != "1":
            environment.update(get_instance_job_envs(instance))
        for key in ENV_KEYS:
            value = os.getenv(key)
            if value and key not in environment:
                environment[key] = value
        environment.update({
            "AYON_PROJECT_NAME": instance.context.data["projectName"],
            "AYON_FOLDER_PATH": instance.data["folderPath"],
            "AYON_TASK_NAME": instance.data["task"],
            "AYON_HOST_NAME": instance.context.data["hostName"],
            "AYON_LOG_NO_COLORS": "1",
        })
        return environment

    def _get_server_name(self, instance):
        server_name = (instance.data.get("deadline") or {}).get("serverName")
        if server_name:
            return server_name
        return instance.context.data["project_settings"]["deadline"][
            "deadline_server"
        ]

    def _get_batch_name(self, instance, product_name, new_version):
        render_job = (
            instance.data.get("publishJobMetadata") or {}
        ).get("job") or {}
        batch_name = (render_job.get("Props") or {}).get("Batch")
        return (
            batch_name
            or instance.data.get("jobBatchName")
            or f"Version Diff - {product_name} v{new_version:03d}"
        )

    def _get_username(self, instance):
        render_job = (
            instance.data.get("publishJobMetadata") or {}
        ).get("job") or {}
        return (
            (render_job.get("Props") or {}).get("User")
            or instance.context.data.get("deadlineUser")
            or getpass.getuser()
        )
