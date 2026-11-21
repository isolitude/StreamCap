from .core.runtime.paths import prepare_user_data_dir, resource_dir, user_data_dir

prepare_user_data_dir()

execute_dir = str(user_data_dir)
resource_dir = str(resource_dir)

__all__ = ["InstallationManager", "execute_dir", "resource_dir"]


def __getattr__(name: str):
    if name == "InstallationManager":
        from .initialization.installation_manager import InstallationManager

        return InstallationManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
