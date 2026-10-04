"""User-initiated local credential setup. Never accept keys in command arguments."""
import getpass
import os
from pathlib import Path
import stat
import sys
import tempfile
import warnings

from .data import CredentialError, _credential_path, _valid_key, credential_status


def save_credential(key, path=None, replace=False):
    """Persist a supplied key outside the project with restricted permissions.

    Existing credentials are not overwritten unless replacement was explicitly
    requested. The caller supplies a test path only in offline unit tests.
    """
    value = _valid_key(key)
    if not value:
        raise CredentialError("API Key 不能为空。")
    target = Path(path) if path is not None else _credential_path()
    parent = target.parent
    if parent.is_symlink() or target.is_symlink():
        raise CredentialError("凭据目录或文件不能是符号链接。")
    if target.exists() and not replace:
        raise CredentialError("本地凭据已存在；需要更新时请明确使用 --replace。")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        if parent.stat().st_uid != os.getuid():
            raise CredentialError("凭据目录不属于当前用户。")
        parent.chmod(0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=parent, delete=False) as handle:
            temporary = Path(handle.name)
            if os.name == "posix":
                os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            handle.write("HITHINK_FINANCE_API_KEY=" + value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, target)
        else:
            # Atomic no-clobber creation; a competing writer cannot be lost.
            os.link(temporary, target)
            temporary.unlink()
        return {"present": True, "source": "user_credential_file", "path": str(target)}
    except OSError:
        raise CredentialError("无法安全写入用户凭据文件，请检查目录权限。") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def configure_interactive(replace=False):
    status = credential_status()
    if status["present"] and not replace:
        return {**status, "reused": True}
    if not sys.stdin.isatty():
        raise CredentialError("请在本机终端运行 ./run.command auth，或双击 connect.command；输入将隐藏，不要把 Key 发到聊天。")
    print("仅将同花顺 API Key 保存在用户凭据目录，不写入项目或日志。")
    print("获取或查看 Key：https://fuyao.aicubes.cn/admin/")
    # Fail instead of falling back to echoed input on unsupported terminals.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            key = getpass.getpass("请粘贴 API Key（输入不会显示）：")
        except (getpass.GetPassWarning, EOFError):
            raise CredentialError("当前终端不支持安全隐藏输入，请使用系统终端。") from None
    try:
        result = save_credential(key, replace=replace)
    finally:
        key = None
    if os.environ.get("HITHINK_FINANCE_API_KEY"):
        result["note"] = "当前进程的环境变量优先于凭据文件；请移除旧环境变量后重启连接。"
    return result
