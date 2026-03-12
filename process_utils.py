import json
import os
import signal


def _load_registry(registry_path: str) -> list[dict]:
    if not os.path.exists(registry_path):
        return []
    try:
        with open(registry_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []


def _save_registry(registry_path: str, items: list[dict]) -> None:
    os.makedirs(os.path.dirname(registry_path), exist_ok=True)
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def register_process(registry_path: str, pid: int, logger) -> None:
    items = [item for item in _load_registry(registry_path) if item.get("pid") != pid]
    items.append({"pid": pid, "pgid": pid})
    _save_registry(registry_path, items)
    logger.info(f"📝 注册任务进程 PID={pid}")


def unregister_process(registry_path: str, pid: int) -> None:
    items = [item for item in _load_registry(registry_path) if item.get("pid") != pid]
    _save_registry(registry_path, items)


def terminate_process_tree(process, logger) -> bool:
    if not process:
        return False
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGTERM)
        logger.info(f"🛑 已终止进程组 PGID={pgid}")
        return True
    except Exception:
        try:
            process.kill()
            logger.info(f"🛑 已终止进程 PID={process.pid}")
            return True
        except Exception as exc:
            logger.warning(f"终止任务失败: {exc}")
            return False


def kill_registered_processes(registry_path: str, logger) -> list[int]:
    killed = []
    items = _load_registry(registry_path)
    if not items:
        return killed
    remaining = []
    for item in items:
        pid = item.get("pid")
        pgid = item.get("pgid", pid)
        if not pgid:
            continue
        try:
            os.killpg(int(pgid), signal.SIGTERM)
            killed.append(int(pgid))
            logger.info(f"🧹 清理遗留进程组 PGID={pgid}")
        except ProcessLookupError:
            continue
        except Exception as exc:
            logger.warning(f"清理遗留进程组失败 PGID={pgid}: {exc}")
            remaining.append(item)
    _save_registry(registry_path, remaining)
    return killed
