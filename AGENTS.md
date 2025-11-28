# Context for Agents

## Critical Bug Fixes: Houdini Cache Submission to Deadline

### Current Issue: Farm Publish Metadata Missing (KeyError ➜ FileNotFoundError)
- **Symptom**: Houdini cache farm publishes fail inside `submit_publish_cache_job.py` when building the metadata payload because `instance.context.data["version"]` is not defined. The `KeyError` stops execution before the metadata JSON is written, so the Deadline worker later dies with `FileNotFoundError` when trying to load `{root[work]}/..._metadata.json`.
- **Planned Fix**: Copy the guarded version lookup already used in `client/ayon_deadline/plugins/publish/global/submit_publish_job.py`: prefer `instance.data.get("version")`, fall back to `instance.context.data.get("version")`, and only include the `version` key when a value exists. Also add a sanity check/log after `create_metadata_path` to ensure the JSON exists before submitting the farm publish job.

### Current Issue: USD Pinning on Farm Missing `pxr`
- **Symptom**: Farm CLI fails while importing `ayon_usd`’s `integrate_pinning_file.py` with `ModuleNotFoundError: No module named 'pxr'`, even though USD pinning works locally.
- **Planned Fix**: Verify `CollectUSDPinningEnvVars` captures Houdini USD env vars (especially `PYTHONPATH`) for cache families, and confirm those envs flow through `FARM_JOB_ENV_DATA_KEY` and `get_instance_job_envs()` into the dependent Deadline publish job (`DeadlineJobInfo.EnvironmentKeyValue`). Add logging/validation if necessary so farm workers inherit the USD paths.

### Bug 1: Missing JSON File (FileNotFoundError) - FIXED
- **Problem**: When publishing multiple cache items/AOVs, the submission script crashed with `KeyError: 'job_info'` after successfully submitting the Deadline job but before creating the metadata JSON file. This left farm jobs looking for a file that was never created.
- **Root Cause**: In `submit_publish_cache_job.py`, the code modified `instance.data["deadline"]` dictionary in-place without creating a copy. On the first iteration, it successfully popped `job_info`. On subsequent iterations, it tried to pop `job_info` again from the same object reference, causing a `KeyError` and crash.
- **Solution**: 
    - Modified `client/ayon_deadline/plugins/publish/houdini/submit_publish_cache_job.py` line 293.
    - Changed `inst["deadline"] = instance.data["deadline"]` to use `deepcopy(instance.data["deadline"])`.
    - This ensures each instance gets its own copy of the deadline dictionary, preventing crashes on multiple iterations.
    - Reference: Correct implementation already existed in `client/ayon_deadline/plugins/publish/global/submit_publish_job.py` line 455.

### Bug 2: Missing USD Module (ModuleNotFoundError: No module named 'pxr') - IMPROVED
- **Problem**: USD-based publishing fails on farm because the `pxr` module is not available in the AYON CLI Python environment.
- **Root Cause**: The AYON CLI uses its own Python environment that doesn't automatically inherit Houdini's USD libraries (`pxr` module) unless specifically configured via `PYTHONPATH`.
- **Solution**:
    - Updated `client/ayon_deadline/plugins/publish/global/collect_usd_pinning_env_vars.py` to include all cache families (`pointcache`, `abc`, `ass`, `redshiftproxy`, `vdbcache`, `model`, `staticMesh`, `camera`, `usdrop`) in addition to `publish.hou`.
    - This ensures the USD environment variable collection plugin runs for all cache types.
    - **Note**: The plugin depends on `ayon_usd` addon and `get_usd_pinning_envs()` function to properly set `PYTHONPATH`. If USD libraries are still missing, verify:
        1. `ayon_usd` addon is installed and enabled.
        2. `get_usd_pinning_envs()` returns the correct Houdini USD library paths.
        3. The plugin is enabled and running (check order: `pyblish.api.CollectorOrder + 0.250`).

## Previous Task: Frames Per Task in Houdini Pointcache Submission
- **Goal**: Enable "Frames per Task" option in the publish dialog for Houdini pointcache (Alembic/bgeo) submissions to Deadline.
- **Problem**: The option appeared for Redshift jobs but not for pointcache jobs.
- **Root Cause**: 
    1. The `HoudiniCacheSubmitDeadline` plugin did not define `get_attribute_defs` for `chunk_size`.
    2. The plugin's `families` list was only `["publish.hou"]`, which is added late during collection. The Publisher UI typically filters plugins based on the instance families present at creation time (e.g., `pointcache`). Because `pointcache` wasn't in the families list, the plugin's attributes (even if defined) wouldn't show up in the UI for the `pointcache` instance.

- **Solution**:
    - Modified `client/ayon_deadline/plugins/publish/houdini/submit_houdini_cache_deadline.py`.
    - Added `NumberDef` import.
    - Added `get_attribute_defs` method.
    - Updated `families` list to include `pointcache` and other cache families (`abc`, `ass`, `redshiftproxy`, `vdbcache`, `model`, `staticMesh`, `camera`) to ensure visibility in the Publisher UI.
    - Updated `get_job_info` to use the attribute value.

## Key Files
- `client/ayon_deadline/plugins/publish/houdini/submit_houdini_cache_deadline.py`: Handles Deadline submission for Houdini cache jobs.
- `client/ayon_deadline/plugins/publish/houdini/submit_publish_cache_job.py`: Creates metadata JSON and submits publish job to Deadline (fixed deepcopy bug).
- `client/ayon_deadline/plugins/publish/global/collect_usd_pinning_env_vars.py`: Collects USD environment variables for farm jobs (updated families list).
- `client/ayon_deadline/plugins/publish/global/submit_publish_job.py`: Reference implementation for correct deepcopy usage.
- `client/ayon_houdini/plugins/publish/collect_cache_farm.py`: Lists the families that get `publish.hou` added for farm submission (used as reference for updating families).

## Dependencies
- `ayon_core.lib.NumberDef`
- `copy.deepcopy` (for preventing dictionary reference issues)
- `ayon_usd` addon (for USD environment variable collection)
