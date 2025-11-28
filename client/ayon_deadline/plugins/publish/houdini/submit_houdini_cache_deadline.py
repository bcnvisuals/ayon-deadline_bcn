import os
from datetime import datetime

from dataclasses import dataclass, field, asdict
import pyblish.api
from ayon_core.lib import (
    is_in_tests,
    NumberDef
)
from ayon_core.pipeline import (
    AYONPyblishPluginMixin
)
from ayon_deadline import abstract_submit_deadline


@dataclass
class HoudiniPluginInfo:
    Build: str = field(default=None)
    IgnoreInputs: bool = field(default=True)
    ScriptJob: bool = field(default=True)
    SceneFile: bool = field(default=None)   # Input
    SaveFile: bool = field(default=True)
    ScriptFilename: str = field(default=None)
    OutputDriver: str = field(default=None)
    Version: str = field(default=None)  # Mandatory for Deadline
    ProjectPath: str = field(default=None)


class HoudiniCacheSubmitDeadline(abstract_submit_deadline.AbstractSubmitDeadline,   # noqa
                                 AYONPyblishPluginMixin):
    """Submit Houdini scene to perform a local publish in Deadline.

    Publishing in Deadline can be helpful for scenes that publish very slow.
    This way it can process in the background on another machine without the
    Artist having to wait for the publish to finish on their local machine.
    """

    label = "Submit Scene to Deadline"
    order = pyblish.api.IntegratorOrder
    hosts = ["houdini"]
    families = [
        "publish.hou",
        "pointcache",
        "abc",
        "ass",
        "redshiftproxy",
        "vdbcache",
        "model",
        "staticMesh",
        "rop.opengl",
        "usdrop",
        "camera"
    ]
    targets = ["local"]
    settings_category = "deadline"

    # Default chunk size for frames per task
    default_chunk_size = 99999

    @classmethod
    def get_attribute_defs(cls):
        """Return attribute definitions for publish dialog UI.

        Returns:
            list: List of attribute definitions for the publish dialog.
        """
        return [
            NumberDef(
                "chunk_size",
                label="Frames Per Task",
                default=cls.default_chunk_size,
                decimals=0,
                minimum=1,
                maximum=99999
            ),
        ]

    def get_job_info(self, job_info=None):
        instance = self._instance
        context = instance.context
        assert all(
            result["success"] for result in context.data["results"]
        ), "Errors found, aborting integration.."

        project_name = instance.context.data["projectName"]
        filepath = context.data["currentFile"]
        scenename = os.path.basename(filepath)
        job_name = "{scene} - {instance} [PUBLISH]".format(
            scene=scenename, instance=instance.name)
        batch_name = f"{project_name} - {scenename}"
        if is_in_tests():
            batch_name += datetime.now().strftime("%d%m%Y%H%M%S")

        job_info.Name = job_name
        job_info.BatchName = batch_name
        job_info.Plugin = instance.data.get("plugin", "Houdini")

        # already collected explicit values for rendered Frames
        if not job_info.Frames:
            frames = "{start}-{end}x{step}".format(
                start=int(instance.data["frameStart"]),
                end=int(instance.data["frameEnd"]),
                step=int(instance.data["byFrameStep"]),
            )

            job_info.Frames = frames

        # When `frames` instance data is a string, it indicates that
        #  the output is a single file.
        # Set the chunk size to a large number because multiple
        #  machines cannot render to the same file.
        if isinstance(instance.data.get("frames"), str):
            job_info.ChunkSize = 99999999
        else:
            # Use chunk_size from attribute values if available,
            # otherwise use the value from job_info (which comes from
            # CollectJobInfo plugin) or fall back to default
            attribute_values = self.get_attr_values_from_data(instance.data)
            chunk_size = attribute_values.get(
                "chunk_size",
                job_info.ChunkSize or self.default_chunk_size
            )
            job_info.ChunkSize = chunk_size

        return job_info

    def get_plugin_info(self):
        # Not all hosts can import this module.
        import hou

        instance = self._instance
        version = hou.applicationVersionString()
        version = ".".join(version.split(".")[:2])
        rop = self.get_rop_node(instance)
        plugin_info = HoudiniPluginInfo(
            Build=None,
            IgnoreInputs=True,
            ScriptJob=True,
            SceneFile=self.scene_path,
            SaveFile=True,
            OutputDriver=rop.path(),
            Version=version,
            ProjectPath=os.path.dirname(self.scene_path)
        )

        plugin_payload = asdict(plugin_info)

        return plugin_payload

    def process(self, instance):
        """Process plugin.
        
        Submit Houdini cache job to Deadline for farm rendering.
        Skips submission if farm rendering is disabled.
        
        Args:
            instance (pyblish.api.Instance): Instance data.
        """
        # Check if farm rendering is actually requested
        if not instance.data.get("farm"):
            self.log.debug("Farm rendering is disabled. Skipping Deadline submission.")
            return

        super(HoudiniCacheSubmitDeadline, self).process(instance)
        output_dir = os.path.dirname(instance.data["files"][0])
        instance.data["outputDir"] = output_dir
        instance.data["toBeRenderedOn"] = "deadline"

    def get_rop_node(self, instance):
        # Not all hosts can import this module.
        import hou

        rop = instance.data.get("instance_node")
        rop_node = hou.node(rop)

        return rop_node
