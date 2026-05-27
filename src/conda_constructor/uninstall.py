import logging
import os
import re
import sys
from pathlib import Path
from shutil import rmtree

from conda.base.constants import COMPATIBLE_SHELLS, PREFIX_FROZEN_FILE, PREFIX_MAGIC_FILE
from conda.base.context import context, reset_context
from conda.cli.main import main as conda_main
from conda.common.compat import on_win
from conda.common.path import win_path_to_unix
from conda.core.initialize import (
    CONDA_INITIALIZE_PS_RE_BLOCK,
    CONDA_INITIALIZE_RE_BLOCK,
    _read_windows_registry,
    make_initialize_plan,
    print_plan_results,
    run_plan,
    run_plan_elevated,
)
from conda.notices.cache import get_notices_cache_dir
from menuinst.cli.cli import install as install_shortcut
from ruamel.yaml import YAML

logger = logging.getLogger()


# =============================================================================
# DEBUG HELPERS - TEMPORARY FOR DEBUGGING MSI UNINSTALL ISSUES
# =============================================================================
def _debug_print(msg: str):
    """Print debug message to both stdout and stderr for visibility."""
    print(f"[DEBUG] {msg}")
    print(f"[DEBUG] {msg}", file=sys.stderr)


def _debug_dump_environment():
    """Dump relevant environment variables for debugging."""
    _debug_print("=" * 60)
    _debug_print("ENVIRONMENT VARIABLE DUMP")
    _debug_print("=" * 60)

    # Key environment variables for path resolution
    key_vars = [
        "USERPROFILE",
        "HOMEPATH",
        "HOMEDRIVE",
        "HOME",
        "USERNAME",
        "USERDOMAIN",
        "COMPUTERNAME",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "ALLUSERSPROFILE",
        "PROGRAMDATA",
        "CONDA_ROOT_PREFIX",
        "CONDA_PREFIX",
        "CONDA_ROOT",
        "CONDA_ROOT_DIR",
    ]

    for var in key_vars:
        value = os.environ.get(var, "<NOT SET>")
        _debug_print(f"  {var}={value}")

    _debug_print("-" * 60)


def _debug_dump_path_resolution():
    """Dump path resolution results for debugging."""
    _debug_print("=" * 60)
    _debug_print("PATH RESOLUTION DUMP")
    _debug_print("=" * 60)

    # Test Path.home()
    try:
        home = Path.home()
        _debug_print(f"  Path.home() = {home}")
        _debug_print(f"  Path.home() exists = {home.exists()}")
    except Exception as e:
        _debug_print(f"  Path.home() FAILED: {type(e).__name__}: {e}")

    # Test os.path.expanduser
    try:
        expanded = os.path.expanduser("~")
        _debug_print(f"  os.path.expanduser('~') = {expanded}")
        _debug_print(f"  expanduser contains '~' = {'~' in expanded}")
    except Exception as e:
        _debug_print(f"  os.path.expanduser('~') FAILED: {type(e).__name__}: {e}")

    # Test Path("~").expanduser()
    try:
        expanded_path = Path("~").expanduser()
        _debug_print(f"  Path('~').expanduser() = {expanded_path}")
    except Exception as e:
        _debug_print(f"  Path('~').expanduser() FAILED: {type(e).__name__}: {e}")

    # Test the actual path we'll use for .conda
    try:
        conda_dir = Path("~/.conda").expanduser()
        _debug_print(f"  Path('~/.conda').expanduser() = {conda_dir}")
        _debug_print(f"  ~/.conda exists = {conda_dir.exists()}")
        if conda_dir.exists():
            _debug_print(f"  ~/.conda is_dir = {conda_dir.is_dir()}")
            try:
                contents = list(conda_dir.iterdir())
                _debug_print(f"  ~/.conda contents = {[str(p) for p in contents]}")
            except Exception as e:
                _debug_print(f"  ~/.conda iterdir FAILED: {e}")
    except Exception as e:
        _debug_print(f"  Path('~/.conda').expanduser() FAILED: {type(e).__name__}: {e}")

    # Test .condarc path
    try:
        condarc = Path("~/.condarc").expanduser()
        _debug_print(f"  Path('~/.condarc').expanduser() = {condarc}")
        _debug_print(f"  ~/.condarc exists = {condarc.exists()}")
    except Exception as e:
        _debug_print(f"  Path('~/.condarc').expanduser() FAILED: {type(e).__name__}: {e}")

    _debug_print("-" * 60)


def _debug_dump_conda_context():
    """Dump conda context information for debugging."""
    _debug_print("=" * 60)
    _debug_print("CONDA CONTEXT DUMP")
    _debug_print("=" * 60)

    try:
        _debug_print(f"  context.root_prefix = {context.root_prefix}")
    except Exception as e:
        _debug_print(f"  context.root_prefix FAILED: {e}")

    try:
        _debug_print(f"  context.conda_prefix = {context.conda_prefix}")
    except Exception as e:
        _debug_print(f"  context.conda_prefix FAILED: {e}")

    try:
        _debug_print(f"  context.target_prefix = {context.target_prefix}")
    except Exception as e:
        _debug_print(f"  context.target_prefix FAILED: {e}")

    try:
        config_files = list(context.config_files)
        _debug_print(f"  context.config_files count = {len(config_files)}")
        for i, cf in enumerate(config_files):
            cf_path = Path(cf) if not isinstance(cf, Path) else cf
            _debug_print(f"    [{i}] {cf_path} (exists={cf_path.exists()})")
    except Exception as e:
        _debug_print(f"  context.config_files FAILED: {e}")

    try:
        _debug_print(f"  context.pkgs_dirs = {context.pkgs_dirs}")
    except Exception as e:
        _debug_print(f"  context.pkgs_dirs FAILED: {e}")

    _debug_print("-" * 60)


def _debug_dump_process_info():
    """Dump process information for debugging."""
    _debug_print("=" * 60)
    _debug_print("PROCESS INFO DUMP")
    _debug_print("=" * 60)
    _debug_print(f"  sys.executable = {sys.executable}")
    _debug_print(f"  sys.platform = {sys.platform}")
    _debug_print(f"  os.getcwd() = {os.getcwd()}")
    _debug_print(f"  os.getpid() = {os.getpid()}")

    if sys.platform == "win32":
        try:
            import ctypes
            _debug_print(f"  IsUserAnAdmin = {ctypes.windll.shell32.IsUserAnAdmin()}")
        except Exception as e:
            _debug_print(f"  IsUserAnAdmin FAILED: {e}")

        try:
            # Get current user via Windows API
            import ctypes
            GetUserNameW = ctypes.windll.advapi32.GetUserNameW
            buffer = ctypes.create_unicode_buffer(256)
            size = ctypes.pointer(ctypes.c_ulong(256))
            GetUserNameW(buffer, size)
            _debug_print(f"  GetUserNameW = {buffer.value}")
        except Exception as e:
            _debug_print(f"  GetUserNameW FAILED: {e}")

    _debug_print("-" * 60)
# =============================================================================
# END DEBUG HELPERS
# =============================================================================
# On Windows, these warnings are expected because the uninstaller may still be
# accessing files (like install.log) that conda cannot rename.
if sys.platform == "win32":
    conda_logger = logging.getLogger("conda.gateways.disk.delete")
    conda_logger.addFilter(lambda record: "Could not remove or rename" not in record.getMessage())


def _remove_file_directory(file: Path, raise_on_error: bool = False):
    """
    Try to remove a file or directory.

    If the file is a link, just unlink, do not remove the target.
    """
    _debug_print(f"_remove_file_directory called with: {file}")
    _debug_print(f"  file type: {type(file)}")
    _debug_print(f"  file str: '{file}'")

    try:
        exists = file.exists()
        _debug_print(f"  file.exists() = {exists}")
        if not exists:
            _debug_print(f"  -> File does not exist, returning early")
            return

        is_dir = file.is_dir()
        is_symlink = file.is_symlink()
        is_file = file.is_file()
        _debug_print(f"  file.is_dir() = {is_dir}")
        _debug_print(f"  file.is_symlink() = {is_symlink}")
        _debug_print(f"  file.is_file() = {is_file}")

        if is_dir:
            _debug_print(f"  -> Calling rmtree({file})")
            rmtree(file)
            _debug_print(f"  -> rmtree completed successfully")
        elif is_symlink or is_file:
            _debug_print(f"  -> Calling file.unlink()")
            file.unlink()
            _debug_print(f"  -> unlink completed successfully")
        else:
            _debug_print(f"  -> File is neither dir, symlink, nor file - skipping")
    except PermissionError as e:
        message = (
            f"Could not remove {file}. "
            "You may need to re-run with elevated privileges or manually remove this file."
        )
        _debug_print(f"  -> PermissionError: {e}")
        if raise_on_error:
            raise PermissionError(message) from e
        else:
            logger.warning(message, exc_info=e)
    except Exception as e:
        _debug_print(f"  -> Unexpected exception: {type(e).__name__}: {e}")
        raise


def _remove_config_file_and_parents(file: Path, raise_on_error: bool = False):
    """
    Remove a configuration file and empty parent directories.

    Only remove the configuration files created by conda.
    For that reason, search only for specific subdirectories
    and search backwards to be conservative about what is deleted.
    """
    _debug_print(f"_remove_config_file_and_parents called with: {file}")
    rootdir = None
    _remove_file_directory(file, raise_on_error=raise_on_error)
    # Directories that may have been created by conda that are okay
    # to be removed if they are empty.
    if file.parent.parts[-1] in (".conda", "conda", "xonsh", "fish"):
        rootdir = file.parent
        _debug_print(f"  rootdir set to parent: {rootdir}")

    # rootdir may be $HOME/%USERPROFILE% if the username is conda, etc.
    try:
        home = Path.home()
        _debug_print(f"  Path.home() = {home}")
    except Exception as e:
        _debug_print(f"  Path.home() FAILED: {e}")
        home = None

    if not rootdir or (home and rootdir == home):
        _debug_print(f"  -> Returning early (rootdir={rootdir}, home={home})")
        return

    # Covers directories like ~/.config/conda/
    if rootdir.parts[-1] in (".config", "conda"):
        rootdir = rootdir.parent
        _debug_print(f"  rootdir adjusted to grandparent: {rootdir}")

    if home and rootdir == home:
        _debug_print(f"  -> Returning early (rootdir equals home)")
        return

    parent = file.parent
    _debug_print(f"  Cleaning empty parent directories starting from: {parent}")
    while parent != rootdir.parent and not next(parent.iterdir(), None):
        _debug_print(f"  Removing empty parent: {parent}")
        _remove_file_directory(parent, raise_on_error=raise_on_error)
        parent = parent.parent


def _requires_init_reverse_hkey(target_key: str, prefixes: list[Path]) -> bool:
    # target_path for cmd.exe is a registry path
    reg_entry, _ = _read_windows_registry(target_key)
    if not isinstance(reg_entry, str):
        return False
    autorun_parts = reg_entry.split("&")
    for env_prefix in prefixes:
        hook = str(env_prefix / "condabin" / "conda_hook.bat")
        if any(hook in part for part in autorun_parts):
            return True
    return False


def _requires_init_reverse_shell(
    target_path: Path, shell: str, prefix: Path, prefixes: list[Path]
) -> bool:
    bin_directory = "Scripts" if on_win else "bin"
    # Only reverse for paths that are outside the uninstall prefix
    # since paths inside the uninstall prefix will be deleted anyway
    if not target_path.exists() or not target_path.is_file() or target_path.is_relative_to(prefix):
        return False
    rc_content = target_path.read_text()
    pattern = CONDA_INITIALIZE_PS_RE_BLOCK if shell == "powershell" else CONDA_INITIALIZE_RE_BLOCK
    flags = re.MULTILINE
    matches = re.findall(pattern, rc_content, flags=flags)
    if not matches:
        return False
    for env_prefix in prefixes:
        # Ignore .exe suffix to make the logic simpler
        if shell in ("csh", "tcsh") and sys.platform != "win32":
            sentinel_str = str(env_prefix / "etc" / "profile.d" / "conda.csh")
        else:
            sentinel_str = str(env_prefix / bin_directory / "conda")
        if sys.platform == "win32" and shell != "powershell":
            # Remove /cygdrive to make the path shell-independent
            sentinel_str = win_path_to_unix(sentinel_str).removeprefix("/cygdrive")
        if any(sentinel_str in match for match in matches):
            return True
    return False


def _get_init_reverse_plan(
    prefix: Path,
    prefixes: list[Path],
    for_user: bool,
    for_system: bool,
    anaconda_prompt: bool,
) -> list[dict]:
    """
    Prepare conda init --reverse runs for the uninstallation.

    Only grab the shells that were initialized by the prefix that
    is to be uninstalled since the shells within the prefix are
    removed later.
    """
    reverse_plan = []
    for shell in COMPATIBLE_SHELLS:
        # Make plan for each shell individually because
        # not every plan includes the shell name
        plan = make_initialize_plan(
            str(prefix),
            [shell],
            for_user,
            for_system,
            anaconda_prompt,
            reverse=True,
        )

        for initializer in plan:
            target_path = initializer["kwargs"]["target_path"]
            append_plan = False
            if target_path.startswith("HKEY"):
                append_plan = _requires_init_reverse_hkey(target_path, prefixes)
            # Ensure that target_path is not empty
            elif target_path:
                append_plan = _requires_init_reverse_shell(
                    Path(target_path), shell, prefix, prefixes
                )
            if append_plan:
                reverse_plan.append(initializer)
    return reverse_plan


def _run_conda_init_reverse(for_user: bool, prefix: Path, prefixes: list[Path]):
    for_system = not for_user
    anaconda_prompt = False
    plan = _get_init_reverse_plan(prefix, prefixes, for_user, for_system, anaconda_prompt)
    # Do not call conda.core.initialize() because it will always run make_install_plan.
    # That function will search for activation scripts in sys.prefix which do no exist
    # in the extraction directory of conda-standalone.
    run_plan(plan)
    try:
        run_plan_elevated(plan)
    except Exception as exc:
        logger.error(
            "Could not revert some shell profiles because they require elevated privileges. "
            "Check the output for lines with `needs sudo` and edit those files manually.",
            exc_info=exc,
        )
    print_plan_results(plan)
    for initializer in plan:
        target_path = initializer["kwargs"]["target_path"]
        if target_path.startswith("HKEY"):
            continue
        target_path = Path(target_path)
        if target_path.exists() and not target_path.read_text().strip():
            _remove_config_file_and_parents(target_path)


def _get_menuinst_base_prefix(prefix: Path, conda_root_prefix: Path | None) -> Path:
    if conda_root_prefix:
        return conda_root_prefix
    # If not set by the user, assume that conda-standalone is in the base environment.
    standalone_path = Path(sys.executable).parent
    if (standalone_path / PREFIX_MAGIC_FILE).exists():
        return standalone_path
    # Fallback: use the uninstallation directory as root_prefix
    return prefix


def _remove_environments(prefix: Path, prefixes: list[Path]):
    # menuinst must be run separately because conda remove --all does not remove all shortcuts.
    # This is because some placeholders depend on conda's context.root_prefix, which is set to
    # the extraction directory of conda-standalone. The base prefix must be determined separately
    # since the uninstallation may be pointed to an environments directory or an extra environment
    # outside of the uninstall prefix.
    if conda_root_prefix := os.environ.get("CONDA_ROOT_PREFIX"):
        conda_root_prefix = Path(conda_root_prefix).resolve()
    default_activation_prefix = context.default_activation_prefix.resolve()
    menuinst_base_prefix = _get_menuinst_base_prefix(prefix, conda_root_prefix).resolve()
    # Uninstalling environments must be performed with the deepest environment first.
    # Otherwise, parent environments will delete the environment directory and
    # uninstallation logic (removing shortcuts, pre-unlink scripts, etc.) cannot be run.
    for env_prefix in reversed(prefixes):
        # Unprotect frozen environments first
        frozen_file = env_prefix / PREFIX_FROZEN_FILE
        if frozen_file.is_file():
            try:
                _remove_file_directory(frozen_file, raise_on_error=True)
            except PermissionError as e:
                raise PermissionError(
                    f"Failed to unprotect '{env_prefix}'. Try to re-run the uninstallation with "
                    f"elevated privileges or remove the file '{frozen_file}' manually.",
                ) from e

        install_shortcut(env_prefix, root_prefix=str(menuinst_base_prefix), remove_shortcuts=[])
        # If conda_root_prefix is the same as prefix, conda remove will not be able
        # to remove that environment, so temporarily unset it.
        if conda_root_prefix and conda_root_prefix == env_prefix:
            del os.environ["CONDA_ROOT_PREFIX"]
            reset_context()
        # Conda does not remove the default environment, so set it to something else temporarily
        if default_activation_prefix == env_prefix:
            os.environ["CONDA_DEFAULT_ACTIVATION_ENV"] = sys.prefix
            reset_context()

        return_code = conda_main("remove", "-y", "-p", str(env_prefix), "--all")
        if return_code != 0:
            raise RuntimeError(f"Failed to remove environment '{env_prefix}'.")

        if conda_root_prefix and conda_root_prefix == env_prefix:
            os.environ["CONDA_ROOT_PREFIX"] = str(conda_root_prefix)
            reset_context()
        if default_activation_prefix == env_prefix:
            del os.environ["CONDA_DEFAULT_ACTIVATION_ENV"]
            reset_context()


def _remove_caches():
    return_code = conda_main("clean", "--all", "-y")
    if return_code != 0:
        logger.warning("Failed to remove all cache files.")
    # Delete empty package cache directories
    for directory in context.pkgs_dirs:
        pkgs_dir = Path(directory)
        if not pkgs_dir.exists():
            continue
        expected_files = [pkgs_dir / "urls", pkgs_dir / "urls.txt"]
        if all(file in expected_files for file in pkgs_dir.iterdir()):
            _remove_file_directory(pkgs_dir)

    notices_dir = Path(get_notices_cache_dir()).expanduser()
    _remove_config_file_and_parents(notices_dir)


def _remove_config_files(remove_config_files: str):
    _debug_print(f"_remove_config_files called with: {remove_config_files}")

    # Debug: show Path.home() value
    try:
        home = Path.home()
        _debug_print(f"  Path.home() = {home}")
    except Exception as e:
        _debug_print(f"  Path.home() FAILED: {e}")
        home = None

    config_files_list = list(context.config_files)
    _debug_print(f"  context.config_files has {len(config_files_list)} entries")

    for i, config_file in enumerate(config_files_list):
        _debug_print(f"  Processing config_file[{i}]: {config_file}")
        if not isinstance(config_file, Path):
            config_file = Path(config_file)
        config_dir = config_file.parent
        _debug_print(f"    config_dir = {config_dir}")
        _debug_print(f"    config_file exists = {config_file.exists()}")

        if remove_config_files == "user" and home:
            try:
                is_relative = config_dir.is_relative_to(home)
                _debug_print(f"    is_relative_to(home) = {is_relative}")
                if not is_relative:
                    _debug_print(f"    -> Skipping (user mode, not relative to home)")
                    continue
            except Exception as e:
                _debug_print(f"    is_relative_to check failed: {e}")

        if remove_config_files == "system" and home:
            try:
                is_relative = config_dir.is_relative_to(home)
                _debug_print(f"    is_relative_to(home) = {is_relative}")
                if is_relative:
                    _debug_print(f"    -> Skipping (system mode, is relative to home)")
                    continue
            except Exception as e:
                _debug_print(f"    is_relative_to check failed: {e}")

        # Skip any configuration files that are relative to CONDA_ROOT or CONDA_PREFIX
        # because they may point to the paths of an activated environment and delete
        # a .condarc file of a different installation. If they point to the installation
        # directory, they have been removed with the environment already.
        conda_dir_env_vars = ("CONDA_ROOT", "CONDA_ROOT_DIR", "CONDA_ROOT_PREFIX", "CONDA_PREFIX")
        skip_due_to_conda_dir = False
        for envvar in conda_dir_env_vars:
            if envvar in os.environ:
                try:
                    envvar_path = Path(os.environ[envvar])
                    is_relative = config_dir.is_relative_to(envvar_path)
                    _debug_print(f"    is_relative_to({envvar}={envvar_path}) = {is_relative}")
                    if is_relative:
                        skip_due_to_conda_dir = True
                        break
                except Exception as e:
                    _debug_print(f"    is_relative_to({envvar}) check failed: {e}")

        if skip_due_to_conda_dir:
            _debug_print(f"    -> Skipping (relative to conda dir)")
            continue

        _debug_print(f"    -> Calling _remove_config_file_and_parents({config_file})")
        _remove_config_file_and_parents(config_file)


def _remove_default_environment_from_configs(prefixes: list[Path]):
    """Remove `default_activation_env` from .condarc files.

    If a named environment is found, issue a warning instead of deleting the entry
    since the named environment may refer to a different installation. To avoid
    excessive warnings, run this function towards the end where fewer .condarc files
    are left to examine.
    """
    yaml = YAML()
    for config_file_str in context.config_files:
        config_file = Path(config_file_str)
        if not config_file.exists():
            continue
        with config_file.open() as crc:
            config = yaml.load(crc)
        if not (default_environment := config.get("default_activation_env")):
            continue
        if "/" in default_environment or (sys.platform == "win32" and "\\" in default_environment):
            if not Path(default_environment).is_relative_to(prefixes[0]):
                continue
            del config["default_activation_env"]
            try:
                if config:
                    with config_file.open(mode="w") as crc:
                        yaml.dump(config, crc)
                else:
                    _remove_config_file_and_parents(config_file, raise_on_error=True)
            except Exception as e:
                print(
                    "WARNING: Unable to remove default activation environment "
                    f"from {config_file}. This may result in broken `conda` installations. "
                    "Please remove `default_activation_env` from the file manually. "
                    f"Traceback: {e}.",
                    file=sys.stderr,
                )
        elif any(default_environment == prefix.name for prefix in prefixes):
            print(
                f"WARNING: Named environment `{default_environment}` is set as "
                f"a default environment in {config_file}. Please ensure that "
                "this environment is available in another existing installation "
                "or remove the `default_activation_env` entry manually from this file.",
                file=sys.stderr,
            )


def uninstall(
    prefix: Path,
    remove_caches: bool = False,
    remove_config_files: str | None = None,
    remove_user_data: bool = False,
) -> None:
    """
    Remove a conda prefix or a directory containing conda environments.

    This command also provides options to remove various cache and configuration
    files to fully remove a conda installation.
    """
    # ==========================================================================
    # DEBUG OUTPUT AT START OF UNINSTALL
    # ==========================================================================
    _debug_print("=" * 70)
    _debug_print("UNINSTALL FUNCTION CALLED")
    _debug_print("=" * 70)
    _debug_print(f"Arguments:")
    _debug_print(f"  prefix = {prefix}")
    _debug_print(f"  remove_caches = {remove_caches}")
    _debug_print(f"  remove_config_files = {remove_config_files}")
    _debug_print(f"  remove_user_data = {remove_user_data}")

    _debug_dump_process_info()
    _debug_dump_environment()
    _debug_dump_path_resolution()
    _debug_dump_conda_context()
    # ==========================================================================

    # See: https://github.com/conda/conda/blob/475e6acbdc98122fcbef4733eb8cb8689324c1c8/conda/gateways/disk/create.py#L482-L488
    envs_dir_magic_file = ".conda_envs_dir_test"

    if not (prefix / PREFIX_MAGIC_FILE).exists() and not (prefix / envs_dir_magic_file).exists():
        raise OSError(f"{prefix} is not a valid conda environment or environments directory.")

    if context.active_prefix:
        active_prefix = Path(context.active_prefix).resolve()
        if active_prefix.is_relative_to(prefix):
            raise OSError(
                f"The currently activated environment is a subdirectory of {prefix}. "
                "Please deactivate the current environment and re-run the uninstallation."
            )

    print(f"Uninstalling conda installation in {prefix}...")
    prefixes = [file.parent.parent.resolve() for file in prefix.glob(f"**/{PREFIX_MAGIC_FILE}")]
    # Sort by path depth. This will place the root prefix first
    # Since it is more likely that profiles contain the root prefix,
    # this makes loops more efficient.
    prefixes.sort(key=lambda x: len(x.parts))

    _debug_print(f"Discovered prefixes: {prefixes}")

    # Run conda --init reverse for the shells
    # that contain a prefix that is being uninstalled
    print("Running conda init --reverse...")
    # Run user and system reversal separately because user
    # and system files may contain separate paths.
    for for_user in (True, False):
        _run_conda_init_reverse(for_user, prefix, prefixes)

    print("Removing environments...")
    _remove_environments(prefix, prefixes)

    # If the uninstall prefix is an environments directory,
    # it should only contain the magic file.
    # On Windows, the directory might still exist if conda-standalone
    # tries to delete itself (it gets renamed to a .conda_trash file).
    # In that case, the directory cannot be deleted - this needs to be
    # done by the uninstaller.
    if prefix.exists() and not any(file.name != envs_dir_magic_file for file in prefix.iterdir()):
        _remove_file_directory(prefix)

    if remove_caches:
        print("Cleaning cache directories.")
        _remove_caches()

    if remove_config_files:
        print("Removing .condarc files...")
        _debug_print(f"About to call _remove_config_files with: {remove_config_files}")
        _remove_config_files(remove_config_files)

    if remove_user_data:
        print("Removing user data...")
        # Debug: show exactly what path we're about to use
        raw_path = Path("~/.conda")
        _debug_print(f"  raw_path (before expanduser) = {raw_path}")
        try:
            expanded_path = raw_path.expanduser()
            _debug_print(f"  expanded_path (after expanduser) = {expanded_path}")
            _debug_print(f"  expanded_path type = {type(expanded_path)}")
            _debug_print(f"  '~' still in expanded_path = {'~' in str(expanded_path)}")
            _debug_print(f"  expanded_path.exists() = {expanded_path.exists()}")
            if expanded_path.exists():
                _debug_print(f"  expanded_path.is_dir() = {expanded_path.is_dir()}")
        except Exception as e:
            _debug_print(f"  expanduser FAILED: {type(e).__name__}: {e}")
            expanded_path = raw_path

        _remove_file_directory(expanded_path)
    else:
        _debug_print("remove_user_data is False, skipping user data removal")

    # Remove default activation environment where possible.
    # Run this at the end because at this point, a lot of
    # configuration files may have already been deleted.
    _debug_print("Calling _remove_default_environment_from_configs...")
    _remove_default_environment_from_configs(prefixes)

    _debug_print("=" * 70)
    _debug_print("UNINSTALL FUNCTION COMPLETED")
    _debug_print("=" * 70)
