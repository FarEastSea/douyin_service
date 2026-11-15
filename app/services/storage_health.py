"""存储维护前确认媒体卷可读；空挂载点不能作为文件丢失的证据。"""
from pathlib import Path
import os


def require_readable_storage(root: Path) -> None:
    if not root.is_dir():
        raise ValueError("下载存储不可用，已停止标记文件缺失，请检查挂载与目录权限")
    excluded = {".quarantine", ".thumbnails", ".locks", "_work_covers", "_author_avatars"}
    def fail(error):
        raise error
    for directory, folders, names in os.walk(root, followlinks=False, onerror=fail):
        folders[:] = [name for name in folders if name not in excluded and not name.startswith(".")]
        for name in names:
            path = Path(directory) / name
            if not name.startswith(".") and not path.is_symlink() and path.is_file():
                with path.open("rb") as handle:
                    handle.read(1)
                return
    raise ValueError("下载目录没有可读取的文件，无法区分文件被清空与磁盘掉挂载，已停止标记任务失败")
