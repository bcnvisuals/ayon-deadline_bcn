# -*- coding: utf-8 -*-
from collections import OrderedDict

import ayon_api
import pyblish.api
from ayon_core.lib import (
    BoolDef,
    NumberDef,
    TextDef,
    EnumDef,
    is_in_tests,
    UISeparatorDef
)
from ayon_core.pipeline.publish import AYONPyblishPluginMixin
from ayon_core.settings import get_project_settings
from ayon_core.lib.profiles_filtering import filter_profiles

from ayon_deadline.lib import FARM_FAMILIES


class CollectJobInfo(pyblish.api.InstancePlugin, AYONPyblishPluginMixin):
    """Collect variables that belong to Deadline's JobInfo.

    Variables like:
    - department
    - priority
    - chunk size

    """

    order = pyblish.api.CollectorOrder + 0.420
    label = "Collect Deadline JobInfo"

    families = FARM_FAMILIES

    def process(self, instance):
        attr_values = self.get_attr_values_from_data(instance.data)
        self.log.info(attr_values)

    @classmethod
    def get_attr_defs_for_instance(cls, create_context, instance):
        if not cls.instance_matches_plugin_families(instance):
            return []

        if not instance["active"]:  # TODO origin_data seem not right
            return []

        project_name = create_context.project_name
        project_settings = get_project_settings(project_name)

        host_name = create_context.host_name

        task_name = instance["task"]
        folder_path = instance["folderPath"]
        folder_entity = ayon_api.get_folder_by_path(project_name,folder_path)
        task_entity = ayon_api.get_task_by_name(
            project_name, folder_entity["id"], task_name)
        profiles = (
            project_settings["deadline"]["publish"][cls.__name__]["profiles"])

        if not profiles:
            return []

        profile = filter_profiles(
            profiles,
            {
                "host_names": host_name,
                "task_types": task_entity["taskType"],
                "task_names": task_name,
                # "product_type": product_type
            }
        )
        overrides = set(profile["overrides"])
        if not profile or not overrides:
            return []

        defs = []

        # should be matching to extract_jobinfo_overrides_enum
        override_defs = OrderedDict({
            "chunkSize": NumberDef(
                "chunkSize",
                label="Frames Per Task",
                default=1,
                decimals=0,
                minimum=1,
                maximum=1000
            ),
            "priority": NumberDef(
                "priority",
                label="Priority",
                decimals=0
            ),
            "department": TextDef(
                "department",
                label="Department",
                default="",
            ),
            "limit_groups": TextDef(
                "limit_groups",
                label="Limit Groups",
                default="",
                placeholder="machine1,machine2"
            ),
            "job_delay": TextDef(
                "job_delay",
                label="Delay job (timecode dd:hh:mm:ss)",
                default=""
            ),
        })

        defs.extend([
            UISeparatorDef("options"),
        ])

        # The Arguments that can be modified by the Publisher
        for key, value in override_defs.items():
            if key not in overrides:
                continue

            default_value = profile[key]
            value.default = default_value
            defs.append(value)

        defs.append(
            UISeparatorDef("sep_alembic_options_end")
        )

        return defs

    @classmethod
    def register_create_context_callbacks(cls, create_context):
        create_context.add_value_changed_callback(cls.on_values_changed)

    @classmethod
    def on_value_change(cls, event):
        for instance_change in event["changes"]:
            if not cls.instance_matches_plugin_families(instance):
                continue
            value_changes = instance_change["changes"]
            if "enabled" not in value_changes:
                continue
            instance = instance_change["instance"]
            new_attrs = cls.get_attr_defs_for_instance(
                event["create_context"], instance
            )
            instance.set_publish_plugin_attr_defs(cls.__name__, new_attrs)


class CollectMayaJobInfo(CollectJobInfo):
    hosts = [
        "maya",
    ]
    @classmethod
    def get_attribute_defs(cls):
        defs = super().get_attribute_defs()

        defs.extend([
            NumberDef(
                "tile_priority",
                label="Tile Assembler Priority",
                decimals=0,
                default=cls.tile_priorit
            ),
            BoolDef(
                "strict_error_checking",
                label="Strict Error Checking",
                default=cls.strict_error_checking
            ),
        ])

        return defs