"""Read-only CAN BLF discovery, profiling, and feature-scoped replay.

The cockpit UI must not animate every frame on a vehicle network.  This module
keeps the filtering rule on the server: a replay is selected by a named cockpit
function and only the CAN IDs/signals that belong to that function are returned.

The service deliberately has no FastAPI dependency.  ``server.py`` can call
``catalog()`` and ``replay()`` from ``asyncio.to_thread`` without coupling the
BLF/DBC implementation to an HTTP transport.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # Kept optional so the rest of the cloud service can still start cleanly.
    import can as _python_can
except ImportError:  # pragma: no cover - exercised in deployments missing extras.
    _python_can = None

try:
    import cantools as _cantools
except ImportError:  # pragma: no cover - exercised in deployments missing extras.
    _cantools = None


class CanLogError(RuntimeError):
    """Base class for recoverable CAN log service errors."""


class CanLogDependencyError(CanLogError):
    """Raised when the optional BLF reader dependency is unavailable."""


class CanLogNotFoundError(CanLogError):
    """Raised when a requested catalog log ID is unknown."""


class CanLogValidationError(CanLogError):
    """Raised for an unsupported profile or invalid replay limit."""


def _profile(
    profile_id: str,
    label: str,
    description: str,
    color: str,
    message_ids: Sequence[int],
    signal_names: Sequence[str],
) -> Dict[str, Any]:
    return {
        "id": profile_id,
        "label": label,
        "description": description,
        "color": color,
        "message_ids": tuple(int(value) for value in message_ids),
        "signal_names": tuple(signal_names),
    }


# These mappings come from the TMS sheet in the supplied communication matrix.
# CAN identifiers are integers parsed from hexadecimal display values, never
# decimal conversions of cells such as "330" or "340".
FEATURE_PROFILES: Tuple[Dict[str, Any], ...] = (
    _profile(
        "climate",
        "空调系统总览",
        "HU 请求、空调反馈、压缩机、TMS 工况和执行器状态",
        "#22D3EE",
        (0x2F8, 0x2F9, 0x330, 0x340, 0x238, 0x248, 0x32C, 0x32D, 0x241, 0x242, 0x2CF),
        (
            "HU_IonizerReq",
            "HU_FrontAUTOACOnReq",
            "HU_FrontACOffReq",
            "HU_ACDEFAdjustReq",
            "HU_ACLEFTempAdjustValueReq",
            "HU_ACFrontModeAdjustReq",
            "HU_ACFanAdjustValueReq",
            "HU_ACReq",
            "HU_CirculationModeReq",
            "AC_FrReqWindLevel",
            "AC_AC_Req",
            "AC_RearDefrosterSwitch",
            "AC_SystemFailureFlag",
            "AC_FrontDefrosterSwitch",
            "AC_FrBlowType",
            "AC_DriverReqTemp",
            "AC_RecycleType",
            "AC_PlasmaSt",
            "CompEnable",
            "CompTargetSpeed",
            "CompSpeed",
            "CompSts",
            "CompFaultCode",
            "TMSOperMod",
            "ACPre",
            "ModelWindMotorReq",
            "RecycleWindMotorReq",
            "HybridWindMotorReq",
            "ModelWindMotorFeedBackVol",
            "RecyclelWindMotorFeedBackVol",
            "HybridWindMotorFeedBackVol",
            "Blower_ErrSta",
            "Comp_CompressorErrSta",
        ),
    ),
    _profile(
        "unlock_active_ventilation",
        "解锁主动换气",
        "0x330 Data[2] bits 1:0；关闭 0x01，开启 0x02",
        "#22D3EE",
        (0x330,),
        ("UnlockActiveVentilation",),
    ),
    _profile(
        "unlock_window_ventilation",
        "解锁降窗通风",
        "0x330 Data[2] bits 3:2；关闭 0x04，开启 0x08",
        "#34D399",
        (0x330,),
        ("UnlockWindowVentilation",),
    ),
    _profile(
        "ac_self_drying",
        "空调自干燥",
        "0x330 Data[2] bits 5:4；关闭 0x10，开启 0x20",
        "#FBBF24",
        (0x330,),
        ("ACSelfDrying",),
    ),
    _profile(
        "front_defrost",
        "前除霜",
        "中控请求与空调控制器反馈的最小闭环",
        "#38BDF8",
        (0x2F9, 0x330),
        ("HU_ACDEFAdjustReq", "AC_FrontDefrosterSwitch", "AC_SystemFailureFlag"),
    ),
    _profile(
        "ac_power",
        "空调开关",
        "AUTO / OFF / A/C 请求、状态及压缩机执行链",
        "#34D399",
        (0x2F9, 0x330, 0x238, 0x248),
        (
            "HU_FrontAUTOACOnReq",
            "HU_FrontACOffReq",
            "HU_ACReq",
            "AC_AC_Req",
            "AC_SystemFailureFlag",
            "CompEnable",
            "CompTargetSpeed",
            "CompSpeed",
            "CompSts",
            "CompFaultCode",
        ),
    ),
    _profile(
        "temperature",
        "温度调节",
        "主驾温度设定请求与空调反馈",
        "#FBBF24",
        (0x2F9, 0x340),
        ("HU_ACLEFTempAdjustValueReq", "AC_DriverReqTemp"),
    ),
    _profile(
        "fan",
        "风量调节",
        "风量请求、实际挡位与鼓风机诊断",
        "#A78BFA",
        (0x2F9, 0x330, 0x2CF),
        ("HU_ACFanAdjustValueReq", "AC_FrReqWindLevel", "Blower_ErrSta"),
    ),
    _profile(
        "airflow_mode",
        "出风模式",
        "出风模式请求、空调反馈和风门执行器",
        "#FB7185",
        (0x2F9, 0x340, 0x241, 0x242),
        (
            "HU_ACFrontModeAdjustReq",
            "AC_FrBlowType",
            "ModelWindMotorReq",
            "ModelWindMotorFeedBackVol",
        ),
    ),
    _profile(
        "circulation",
        "内外循环",
        "循环模式请求、状态及循环风门反馈",
        "#2DD4BF",
        (0x2F9, 0x340, 0x241, 0x242),
        (
            "HU_CirculationModeReq",
            "AC_RecycleType",
            "RecycleWindMotorReq",
            "RecyclelWindMotorFeedBackVol",
        ),
    ),
    _profile(
        "ionizer",
        "空气净化",
        "离子发生器请求与等离子状态反馈",
        "#60A5FA",
        (0x2F8, 0x340),
        ("HU_IonizerReq", "AC_PlasmaSt"),
    ),
)


SIGNAL_LABELS: Mapping[str, str] = {
    "UnlockActiveVentilation": "解锁主动换气",
    "UnlockWindowVentilation": "解锁降窗通风",
    "ACSelfDrying": "空调自干燥",
    "HU_IonizerReq": "离子发生器请求",
    "HU_FrontAUTOACOnReq": "前排 AUTO 请求",
    "HU_FrontACOffReq": "前排空调关闭请求",
    "HU_ACDEFAdjustReq": "前除霜请求",
    "HU_ACLEFTempAdjustValueReq": "主驾温度请求",
    "HU_ACFrontModeAdjustReq": "出风模式请求",
    "HU_ACFanAdjustValueReq": "风量请求",
    "HU_ACReq": "A/C 请求",
    "HU_CirculationModeReq": "循环模式请求",
    "AC_FrReqWindLevel": "前排实际风量",
    "AC_AC_Req": "A/C 状态",
    "AC_RearDefrosterSwitch": "后除霜状态",
    "AC_SystemFailureFlag": "空调系统故障",
    "AC_FrontDefrosterSwitch": "前除霜状态",
    "AC_FrBlowType": "实际出风模式",
    "AC_DriverReqTemp": "主驾设定温度",
    "AC_RecycleType": "实际循环模式",
    "AC_PlasmaSt": "等离子状态",
    "CompEnable": "压缩机使能",
    "CompTargetSpeed": "压缩机目标转速",
    "CompSpeed": "压缩机转速",
    "CompSts": "压缩机状态",
    "CompFaultCode": "压缩机故障码",
    "TMSOperMod": "TMS 工作模式",
    "ACPre": "空调压力",
    "ModelWindMotorReq": "模式风门请求",
    "RecycleWindMotorReq": "循环风门请求",
    "HybridWindMotorReq": "混合风门请求",
    "ModelWindMotorFeedBackVol": "模式风门反馈",
    "RecyclelWindMotorFeedBackVol": "循环风门反馈",
    "HybridWindMotorFeedBackVol": "混合风门反馈",
    "Blower_ErrSta": "鼓风机故障",
    "Comp_CompressorErrSta": "压缩机诊断",
}


# Reverse-engineered from the supplied real-car video and BLF.  These fields
# intentionally live beside (not inside) the DBC because the matrix does not
# define their signal names.
CUSTOM_RAW_SIGNALS: Mapping[int, Tuple[Mapping[str, Any], ...]] = {
    0x330: (
        {"name": "UnlockActiveVentilation", "byte": 2, "mask": 0x03, "shift": 0},
        {"name": "UnlockWindowVentilation", "byte": 2, "mask": 0x0C, "shift": 2},
        {"name": "ACSelfDrying", "byte": 2, "mask": 0x30, "shift": 4},
    ),
}


MESSAGE_FLOW: Mapping[int, Mapping[str, Any]] = {
    0x2F8: {"sender": "HU / GW", "receivers": ("TMS",), "direction": "request"},
    0x2F9: {"sender": "HU / GW", "receivers": ("TMS",), "direction": "request"},
    0x330: {"sender": "AC", "receivers": ("TMS", "HU"), "direction": "feedback"},
    0x340: {"sender": "AC", "receivers": ("TMS", "HU"), "direction": "feedback"},
    0x238: {"sender": "TMS", "receivers": ("COMPRESSOR",), "direction": "command"},
    0x248: {"sender": "COMPRESSOR", "receivers": ("TMS",), "direction": "feedback"},
    0x32C: {"sender": "TMS", "receivers": ("GW", "HU"), "direction": "state"},
    0x32D: {"sender": "TMS", "receivers": ("GW",), "direction": "state"},
    0x241: {"sender": "TMS1", "receivers": ("TMS2",), "direction": "command"},
    0x242: {"sender": "TMS2", "receivers": ("TMS1",), "direction": "feedback"},
    0x2CF: {"sender": "TMS", "receivers": ("VCU",), "direction": "diagnostic"},
}


def _default_path(base_dir: Path, child_name: str) -> Path:
    """Prefer a child directory, then the repository's sibling data directory."""

    child = base_dir / child_name
    if child.exists():
        return child
    return base_dir.parent / child_name


def _format_can_id(value: int) -> str:
    return "0x{:X}".format(int(value))


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    # cantools NamedSignalValue exposes a useful name/string representation.
    if hasattr(value, "name"):
        return str(getattr(value, "name"))
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isfinite(numeric):
        return int(numeric) if numeric.is_integer() else numeric
    return None


class _DbcCollection:
    """Small multi-file wrapper that preserves each message's DBC source."""

    def __init__(self, sources: Sequence[Tuple[Path, Any]]) -> None:
        self.sources = tuple(sources)
        self.messages = tuple(
            message
            for _path, database in self.sources
            for message in (getattr(database, "messages", ()) or ())
        )
        self._by_frame_id: Dict[int, List[Tuple[Any, str]]] = {}
        for path, database in self.sources:
            for message in (getattr(database, "messages", ()) or ()):
                frame_id = int(getattr(message, "frame_id"))
                self._by_frame_id.setdefault(frame_id, []).append((message, path.name))

    def candidates(self, frame_id: int) -> List[Tuple[Any, str]]:
        return list(self._by_frame_id.get(int(frame_id), ()))

    @property
    def duplicate_frame_ids(self) -> List[int]:
        return sorted(frame_id for frame_id, values in self._by_frame_id.items() if len(values) > 1)


class CanLogService:
    """Discover BLFs and build feature-scoped, JSON-ready replays."""

    def __init__(
        self,
        base_dir: Optional[os.PathLike[str] | str] = None,
        *,
        log_dir: Optional[os.PathLike[str] | str] = None,
        dbc_path: Optional[os.PathLike[str] | str] = None,
        matrix_path: Optional[os.PathLike[str] | str] = None,
        reader_factory: Optional[Callable[[str], Iterable[Any]]] = None,
        database: Any = None,
    ) -> None:
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent).resolve()
        default_log_dir = _default_path(self.base_dir, "实车log")
        default_dbc_dir = _default_path(self.base_dir, "实车DBC")
        self.log_dir = Path(log_dir or os.getenv("CAN_LOG_DIR") or default_log_dir).resolve()
        self.dbc_path = Path(
            dbc_path
            or os.getenv("CAN_DBC_PATH")
            or default_dbc_dir
        ).resolve()
        self.matrix_path = Path(
            matrix_path
            or os.getenv("CAN_MATRIX_PATH")
            or (default_dbc_dir / "通信矩阵" / "2028Pch05_整合通信矩阵.xlsx")
        ).resolve()
        self._reader_factory = reader_factory
        self._database = database
        self._database_injected = database is not None
        self._database_loaded = database is not None
        self._database_fingerprint: Optional[Tuple[Tuple[str, int, int], ...]] = None
        self._database_error: Optional[str] = None
        self._log_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._profiles = {profile["id"]: profile for profile in FEATURE_PROFILES}

    def _open_reader(self, path: Path) -> Iterable[Any]:
        if self._reader_factory is not None:
            return self._reader_factory(str(path))
        if _python_can is None:
            raise CanLogDependencyError(
                "BLF 回放缺少 python-can；请安装 requirements.txt 或执行 "
                "`python -m pip install python-can`."
            )
        return _python_can.BLFReader(str(path))

    @staticmethod
    def _close_reader(reader: Any) -> None:
        for method_name in ("stop", "close"):
            method = getattr(reader, method_name, None)
            if callable(method):
                method()
                return

    def _discover_dbc_files(self) -> List[Path]:
        if self.dbc_path.is_file():
            return [self.dbc_path] if self.dbc_path.suffix.lower() == ".dbc" else []
        if not self.dbc_path.is_dir():
            return []
        root = self.dbc_path.resolve()
        files: List[Path] = []
        for path in self.dbc_path.rglob("*"):
            if not path.is_file() or path.suffix.lower() != ".dbc":
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            files.append(resolved)
        return sorted(files, key=lambda value: str(value).casefold())

    @staticmethod
    def _dbc_fingerprint(paths: Sequence[Path]) -> Tuple[Tuple[str, int, int], ...]:
        values: List[Tuple[str, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            values.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
        return tuple(values)

    def _load_database(self, *, refresh: bool = False) -> Any:
        with self._lock:
            if self._database_injected:
                return self._database
            if self._database_loaded and not refresh:
                return self._database
            dbc_files = self._discover_dbc_files()
            fingerprint = self._dbc_fingerprint(dbc_files)
            if self._database_loaded and fingerprint == self._database_fingerprint:
                return self._database
            self._database_loaded = True
            self._database_fingerprint = fingerprint
            self._database = None
            self._database_error = None
            if not dbc_files:
                self._database_error = "DBC 目录中未找到 .dbc 文件：{}".format(self.dbc_path)
                return None
            if _cantools is None:
                self._database_error = (
                    "DBC 解码缺少 cantools；请安装 requirements.txt 或执行 "
                    "`python -m pip install cantools`."
                )
                return None
            sources: List[Tuple[Path, Any]] = []
            errors: List[str] = []
            for path in dbc_files:
                try:
                    database = _cantools.database.load_file(str(path), strict=False)
                    sources.append((path, database))
                except Exception as exc:  # One bad DBC must not block the other files.
                    errors.append("{}：{}".format(path.name, exc))
            if sources:
                self._database = _DbcCollection(sources)
            if errors:
                self._database_error = "部分 DBC 加载失败：{}".format("；".join(errors))
            elif not sources:
                self._database_error = "DBC 文件均无法加载"
            return self._database

    def _message_for_id(
        self, can_id: int, allowed_signal_names: Sequence[str] = ()
    ) -> Tuple[Any, Optional[str]]:
        database = self._load_database()
        if database is None:
            return None, None
        if isinstance(database, _DbcCollection):
            candidates = database.candidates(can_id)
            if not candidates:
                return None, None
            allowed = set(allowed_signal_names)
            if allowed and len(candidates) > 1:
                candidates.sort(
                    key=lambda item: len({
                        str(getattr(signal, "name", ""))
                        for signal in (getattr(item[0], "signals", ()) or ())
                    } & allowed),
                    reverse=True,
                )
            return candidates[0]
        try:
            return database.get_message_by_frame_id(can_id), "injected DBC"
        except (KeyError, ValueError):
            return None, None

    @staticmethod
    def _log_id(path: Path) -> str:
        return hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:20]

    def _discover_logs(self) -> List[Path]:
        if not self.log_dir.is_dir():
            return []
        root = self.log_dir.resolve()
        logs: List[Path] = []
        for path in self.log_dir.iterdir():
            if not path.is_file() or path.suffix.lower() != ".blf":
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            logs.append(resolved)
        return sorted(logs, key=lambda item: (-item.stat().st_mtime_ns, item.name.casefold()))

    @staticmethod
    def _message_channel(message: Any) -> str:
        channel = getattr(message, "channel", None)
        return "0" if channel is None else str(channel)

    @staticmethod
    def _usable_can_message(message: Any) -> bool:
        return (
            hasattr(message, "arbitration_id")
            and not bool(getattr(message, "is_error_frame", False))
            and not bool(getattr(message, "is_remote_frame", False))
        )

    def _scan_log(self, path: Path) -> Dict[str, Any]:
        stat = path.stat()
        fingerprint = (stat.st_size, stat.st_mtime_ns)
        cache_key = str(path)
        cached = self._log_cache.get(cache_key)
        if cached and cached["fingerprint"] == fingerprint:
            return copy.deepcopy(cached["stats"])

        total_frames = 0
        error_frames = 0
        first_timestamp: Optional[float] = None
        last_timestamp: Optional[float] = None
        channels: set[str] = set()
        id_counts: Counter[int] = Counter()
        reader = self._open_reader(path)
        try:
            for message in reader:
                total_frames += 1
                timestamp = getattr(message, "timestamp", None)
                if isinstance(timestamp, (int, float)) and math.isfinite(float(timestamp)):
                    numeric_timestamp = float(timestamp)
                    if first_timestamp is None:
                        first_timestamp = numeric_timestamp
                    last_timestamp = numeric_timestamp
                channels.add(self._message_channel(message))
                if bool(getattr(message, "is_error_frame", False)):
                    error_frames += 1
                if self._usable_can_message(message):
                    id_counts[int(message.arbitration_id)] += 1
        finally:
            self._close_reader(reader)

        profile_counts = {
            profile_id: sum(id_counts[value] for value in profile["message_ids"])
            for profile_id, profile in self._profiles.items()
        }
        recommended_features = [
            profile_id
            for profile_id, _ in sorted(
                profile_counts.items(), key=lambda item: (-item[1], item[0])
            )
            if profile_counts[profile_id] > 0
        ]
        duration_ms = 0
        if first_timestamp is not None and last_timestamp is not None:
            duration_ms = max(0, int(round((last_timestamp - first_timestamp) * 1000)))
        stats = {
            "id": self._log_id(path),
            "name": path.name,
            "size_bytes": int(stat.st_size),
            "modified_at_ms": int(stat.st_mtime * 1000),
            "duration_ms": duration_ms,
            "total_frames": total_frames,
            "error_frames": error_frames,
            "channels": sorted(channels),
            "profile_counts": profile_counts,
            "recommended_features": recommended_features,
            "status": "ready",
            # Internal-only scan controls used by replay; stripped from catalog.
            "_path": str(path),
            "_id_counts": dict(id_counts),
            "_first_timestamp": first_timestamp,
        }
        self._log_cache[cache_key] = {"fingerprint": fingerprint, "stats": copy.deepcopy(stats)}
        return stats

    @staticmethod
    def _public_log(stats: Mapping[str, Any]) -> Dict[str, Any]:
        return {key: copy.deepcopy(value) for key, value in stats.items() if not key.startswith("_")}

    def _matrix_metadata(self) -> Dict[str, Any]:
        if not self.matrix_path.is_file():
            return {"status": "missing", "name": self.matrix_path.name}
        stat = self.matrix_path.stat()
        status = "available"
        file_format = "xlsx"
        try:
            with self.matrix_path.open("rb") as handle:
                header = handle.read(8)
            if header.startswith(b"\xd0\xcf\x11\xe0"):
                # The supplied matrix is an encrypted OOXML package in an OLE
                # container.  Runtime signal decoding intentionally uses the
                # derived DBC and never stores or returns the workbook password.
                status = "encrypted"
                file_format = "encrypted-ooxml"
            elif header.startswith(b"PK"):
                file_format = "xlsx"
        except OSError:
            status = "unreadable"
        return {
            "status": status,
            "name": self.matrix_path.name,
            "format": file_format,
            "size_bytes": int(stat.st_size),
            "modified_at_ms": int(stat.st_mtime * 1000),
        }

    def _dbc_metadata(self) -> Dict[str, Any]:
        database = self._load_database(refresh=True)
        if database is not None:
            if isinstance(database, _DbcCollection):
                files = [
                    {
                        "name": path.name,
                        "message_count": len(getattr(source, "messages", ()) or ()),
                        "size_bytes": int(path.stat().st_size),
                        "modified_at_ms": int(path.stat().st_mtime * 1000),
                    }
                    for path, source in database.sources
                ]
                duplicate_ids = [
                    _format_can_id(value) for value in database.duplicate_frame_ids
                ]
            else:
                files = [{
                    "name": "injected DBC",
                    "message_count": len(getattr(database, "messages", ()) or ()),
                }]
                duplicate_ids = []
            result = {
                "status": "loaded",
                "name": self.dbc_path.name,
                "directory": str(self.dbc_path if self.dbc_path.is_dir() else self.dbc_path.parent),
                "file_count": len(files),
                "files": files,
                "message_count": len(getattr(database, "messages", ()) or ()),
                "duplicate_ids": duplicate_ids,
            }
            if self._database_error:
                result["detail"] = self._database_error
            return result
        return {
            "status": "unavailable",
            "name": self.dbc_path.name,
            "directory": str(self.dbc_path if self.dbc_path.is_dir() else self.dbc_path.parent),
            "file_count": 0,
            "files": [],
            "message_count": 0,
            "detail": self._database_error or "DBC 未加载",
        }

    def _public_profiles(self, logs: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        values: List[Dict[str, Any]] = []
        for profile in FEATURE_PROFILES:
            best_log = max(logs, key=lambda log: log["profile_counts"][profile["id"]], default=None)
            best_count = (
                int(best_log["profile_counts"][profile["id"]]) if best_log is not None else 0
            )
            values.append({
                "id": profile["id"],
                "label": profile["label"],
                "description": profile["description"],
                "color": profile["color"],
                "message_ids": [_format_can_id(value) for value in profile["message_ids"]],
                "signal_names": list(profile["signal_names"]),
                "recommended_log_id": best_log["id"] if best_log is not None and best_count else None,
                "available_frames": best_count,
            })
        return values

    def catalog(self) -> Dict[str, Any]:
        """Return profiles and current BLF inventory, discovering new files each call."""

        with self._lock:
            paths = self._discover_logs()
            current_paths = {str(path) for path in paths}
            for stale_path in set(self._log_cache) - current_paths:
                self._log_cache.pop(stale_path, None)

            logs: List[Dict[str, Any]] = []
            for path in paths:
                try:
                    logs.append(self._scan_log(path))
                except CanLogDependencyError:
                    raise
                except Exception as exc:
                    stat = path.stat()
                    logs.append({
                        "id": self._log_id(path),
                        "name": path.name,
                        "size_bytes": int(stat.st_size),
                        "modified_at_ms": int(stat.st_mtime * 1000),
                        "duration_ms": 0,
                        "total_frames": 0,
                        "error_frames": 0,
                        "channels": [],
                        "profile_counts": {profile_id: 0 for profile_id in self._profiles},
                        "recommended_features": [],
                        "status": "error",
                        "detail": str(exc),
                        "_path": str(path),
                        "_id_counts": {},
                        "_first_timestamp": None,
                    })

            ready_logs = [log for log in logs if log.get("status") == "ready"]
            default_log = max(
                ready_logs,
                key=lambda log: log["profile_counts"].get("climate", 0),
                default=None,
            )
            if default_log and not default_log["profile_counts"].get("climate", 0):
                default_log = ready_logs[0] if ready_logs else None
            public_logs = [self._public_log(log) for log in logs]
            return {
                "log_directory": str(self.log_dir),
                "default_feature_id": "climate",
                "default_log_id": default_log["id"] if default_log else None,
                "features": self._public_profiles(public_logs),
                "logs": public_logs,
                "matrix": self._matrix_metadata(),
                "dbc": self._dbc_metadata(),
            }

    def _resolve_log(self, log_id: str) -> Tuple[Path, Dict[str, Any]]:
        if not isinstance(log_id, str) or not log_id or len(log_id) > 128:
            raise CanLogNotFoundError("未找到指定 BLF 日志")
        for path in self._discover_logs():
            if self._log_id(path) != log_id:
                continue
            root = self.log_dir.resolve()
            resolved = path.resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:  # Defensive even though discovery already enforces it.
                raise CanLogNotFoundError("BLF 日志路径无效") from exc
            return resolved, self._scan_log(resolved)
        raise CanLogNotFoundError("未找到指定 BLF 日志；请刷新日志目录")

    @staticmethod
    def _message_metadata(message_definition: Any, can_id: int) -> Tuple[str, str, List[str], str]:
        flow = MESSAGE_FLOW.get(can_id, {})
        message_name = getattr(message_definition, "name", None) or "CAN_{}".format(
            _format_can_id(can_id)
        )
        senders = list(getattr(message_definition, "senders", ()) or ())
        sender = str(flow.get("sender") or (senders[0] if senders else "UNKNOWN"))
        receivers: List[str] = list(flow.get("receivers", ()))
        if not receivers and message_definition is not None:
            seen: set[str] = set()
            for signal in getattr(message_definition, "signals", ()) or ():
                for receiver in getattr(signal, "receivers", ()) or ():
                    if receiver not in seen:
                        receivers.append(str(receiver))
                        seen.add(str(receiver))
        direction = str(flow.get("direction") or "bus")
        return str(message_name), sender, receivers, direction

    @staticmethod
    def _decode_values(message_definition: Any, data: bytes) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
        if message_definition is None:
            return {}, {}
        try:
            values = message_definition.decode(
                data, decode_choices=False, scaling=True, allow_truncated=True
            )
        except TypeError:  # Small fake/test definitions may not accept all keywords.
            values = message_definition.decode(data, decode_choices=False)
        except Exception:
            return {}, {}
        try:
            choices = message_definition.decode(
                data, decode_choices=True, scaling=True, allow_truncated=True
            )
        except TypeError:
            try:
                choices = message_definition.decode(data, decode_choices=True)
            except Exception:
                choices = {}
        except Exception:
            choices = {}
        return values or {}, choices or {}

    def _normalise_frame(
        self,
        message: Any,
        profile: Mapping[str, Any],
        *,
        sequence: int,
        first_relevant_timestamp: float,
        previous_signals: Dict[Tuple[int, str], Tuple[Any, Any]],
        previous_data: Dict[int, str],
    ) -> Dict[str, Any]:
        can_id = int(message.arbitration_id)
        data = bytes(getattr(message, "data", b"") or b"")
        data_hex = " ".join("{:02X}".format(value) for value in data)
        definition, dbc_source = self._message_for_id(can_id, profile["signal_names"])
        message_name, sender, receivers, direction = self._message_metadata(definition, can_id)
        cycle_time = getattr(definition, "cycle_time", None)
        values, choices = self._decode_values(definition, data)
        values = dict(values)
        choices = dict(choices)
        allowed_signals = set(profile["signal_names"])
        custom_decoded = False
        for field in CUSTOM_RAW_SIGNALS.get(can_id, ()):
            signal_name = str(field["name"])
            byte_index = int(field["byte"])
            if signal_name not in allowed_signals or byte_index >= len(data):
                continue
            raw_value = (data[byte_index] & int(field["mask"])) >> int(field["shift"])
            values[signal_name] = raw_value
            choices[signal_name] = {1: "关闭", 2: "开启"}.get(raw_value)
            custom_decoded = True
        if custom_decoded:
            dbc_source = "实车日志逆向映射（0x330 Data[2]）"
        definition_signals = {
            str(getattr(signal, "name", "")): signal
            for signal in (getattr(definition, "signals", ()) or ())
        }
        signals: List[Dict[str, Any]] = []
        any_signal_changed = False
        for signal_name, raw_value in values.items():
            signal_name = str(signal_name)
            if signal_name not in allowed_signals:
                continue
            value = _json_scalar(raw_value)
            choice_value = choices.get(signal_name)
            text: Optional[str] = None
            if choice_value is not None and not isinstance(choice_value, (int, float, bool)):
                candidate = _json_scalar(choice_value)
                if candidate is not None and str(candidate) != str(value):
                    text = str(candidate)
            signal_definition = definition_signals.get(signal_name)
            unit = str(getattr(signal_definition, "unit", None) or "")
            state_key = (can_id, signal_name)
            signature = (value, text)
            changed = state_key in previous_signals and previous_signals[state_key] != signature
            previous_signals[state_key] = signature
            any_signal_changed = any_signal_changed or changed
            signals.append({
                "name": signal_name,
                "label": SIGNAL_LABELS.get(signal_name, signal_name),
                "value": value,
                "text": text,
                "unit": unit,
                "changed": changed,
            })

        raw_changed = can_id in previous_data and previous_data[can_id] != data_hex
        previous_data[can_id] = data_hex
        timestamp = float(getattr(message, "timestamp", first_relevant_timestamp))
        return {
            "offset_ms": max(0, int(round((timestamp - first_relevant_timestamp) * 1000))),
            "can_id": _format_can_id(can_id),
            "message_name": message_name,
            "cycle_ms": int(cycle_time) if isinstance(cycle_time, (int, float)) else None,
            "data_hex": data_hex,
            "dlc": int(getattr(message, "dlc", len(data)) or len(data)),
            "channel": self._message_channel(message),
            "sender": sender,
            "receivers": receivers,
            "direction": direction,
            "dbc_source": dbc_source,
            "signals": signals,
            "changed": bool(any_signal_changed or raw_changed),
            "sequence": sequence,
        }

    @staticmethod
    def _sampling_positions(
        counts: Mapping[int, int], limit: int
    ) -> Dict[int, set[int]]:
        """Allocate bounded samples per CAN ID so periodic IDs cannot alias away."""

        positive = {int(can_id): int(count) for can_id, count in counts.items() if count > 0}
        total = sum(positive.values())
        if total <= limit:
            return {can_id: set(range(count)) for can_id, count in positive.items()}

        ordered_ids = sorted(positive, key=lambda can_id: (-positive[can_id], can_id))
        if limit < len(ordered_ids):
            return {can_id: {0} for can_id in ordered_ids[:limit]}

        quotas = {can_id: 1 for can_id in ordered_ids}
        remaining = limit - len(ordered_ids)
        capacities = {can_id: positive[can_id] - 1 for can_id in ordered_ids}
        capacity_total = sum(capacities.values())
        fractional: List[Tuple[float, int]] = []
        if remaining > 0 and capacity_total > 0:
            for can_id in ordered_ids:
                exact = remaining * capacities[can_id] / float(capacity_total)
                extra = min(capacities[can_id], int(math.floor(exact)))
                quotas[can_id] += extra
                fractional.append((exact - extra, can_id))
            leftover = limit - sum(quotas.values())
            for _, can_id in sorted(fractional, key=lambda item: (-item[0], item[1])):
                if leftover <= 0:
                    break
                if quotas[can_id] >= positive[can_id]:
                    continue
                quotas[can_id] += 1
                leftover -= 1

        positions: Dict[int, set[int]] = {}
        for can_id in ordered_ids:
            count = positive[can_id]
            quota = min(count, quotas[can_id])
            if quota <= 1:
                positions[can_id] = {0}
            else:
                positions[can_id] = {
                    int(round(index * (count - 1) / float(quota - 1)))
                    for index in range(quota)
                }
        return positions

    def replay(self, log_id: str, feature_id: str, limit: int = 4000) -> Dict[str, Any]:
        """Return a bounded chronological replay containing only one function."""

        if feature_id not in self._profiles:
            raise CanLogValidationError("未知 CAN 功能：{}".format(feature_id))
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise CanLogValidationError("limit 必须是 1 到 10000 之间的整数")
        with self._lock:
            path, stats = self._resolve_log(log_id)
            if stats.get("status") != "ready":
                raise CanLogError("BLF 日志当前不可用")
            profile = self._profiles[feature_id]
            self._load_database(refresh=True)
            relevant_ids = set(profile["message_ids"])
            matched_count = int(stats["profile_counts"].get(feature_id, 0))
            if matched_count <= 0:
                public_profile = next(
                    item
                    for item in self._public_profiles([self._public_log(stats)])
                    if item["id"] == feature_id
                )
                return {
                    "feature": public_profile,
                    "source_file": self._public_log(stats),
                    "matched_frames": 0,
                    "returned_frames": 0,
                    "sampled": False,
                    "source_offset_ms": 0,
                    "frames": [],
                    "warnings": ["所选日志不包含该功能的相关 CAN ID"],
                }

            id_counts = {
                can_id: int(stats.get("_id_counts", {}).get(can_id, 0))
                for can_id in relevant_ids
                if int(stats.get("_id_counts", {}).get(can_id, 0)) > 0
            }
            sampling_positions = self._sampling_positions(id_counts, limit)
            selected: List[Any] = []
            first_relevant_timestamp: Optional[float] = None
            per_id_index: Counter[int] = Counter()
            reader = self._open_reader(path)
            try:
                for message in reader:
                    if not self._usable_can_message(message):
                        continue
                    if int(message.arbitration_id) not in relevant_ids:
                        continue
                    if first_relevant_timestamp is None:
                        first_relevant_timestamp = float(getattr(message, "timestamp", 0.0))
                    can_id = int(message.arbitration_id)
                    local_index = per_id_index[can_id]
                    if local_index in sampling_positions.get(can_id, set()):
                        selected.append(message)
                    per_id_index[can_id] += 1
            finally:
                self._close_reader(reader)

            selected.sort(key=lambda message: float(getattr(message, "timestamp", 0.0)))

            first_relevant_timestamp = first_relevant_timestamp or 0.0
            previous_signals: Dict[Tuple[int, str], Tuple[Any, Any]] = {}
            previous_data: Dict[int, str] = {}
            frames = [
                self._normalise_frame(
                    message,
                    profile,
                    sequence=index + 1,
                    first_relevant_timestamp=first_relevant_timestamp,
                    previous_signals=previous_signals,
                    previous_data=previous_data,
                )
                for index, message in enumerate(selected)
            ]
            public_profiles = self._public_profiles([self._public_log(stats)])
            public_profile = next(item for item in public_profiles if item["id"] == feature_id)
            log_first_timestamp = stats.get("_first_timestamp")
            source_offset_ms = 0
            if isinstance(log_first_timestamp, (int, float)):
                source_offset_ms = max(
                    0, int(round((first_relevant_timestamp - float(log_first_timestamp)) * 1000))
                )
            warnings: List[str] = []
            if self._database_error:
                warnings.append(self._database_error)
            return {
                "feature": public_profile,
                "source_file": self._public_log(stats),
                "matched_frames": matched_count,
                "returned_frames": len(frames),
                "sampled": len(frames) < matched_count,
                "sampling_plan": {
                    _format_can_id(can_id): len(positions)
                    for can_id, positions in sorted(sampling_positions.items())
                },
                "source_offset_ms": source_offset_ms,
                "frames": frames,
                "warnings": warnings,
            }


_SERVICE_CACHE: Dict[str, CanLogService] = {}
_SERVICE_CACHE_LOCK = threading.Lock()


def get_can_log_service(
    base_dir: Optional[os.PathLike[str] | str] = None,
) -> CanLogService:
    """Return one cached service per project base directory."""

    resolved = Path(base_dir or Path(__file__).resolve().parent).resolve()
    key = str(resolved)
    with _SERVICE_CACHE_LOCK:
        service = _SERVICE_CACHE.get(key)
        if service is None:
            service = CanLogService(base_dir=resolved)
            _SERVICE_CACHE[key] = service
        return service


__all__ = [
    "CanLogDependencyError",
    "CanLogError",
    "CanLogNotFoundError",
    "CanLogService",
    "CanLogValidationError",
    "FEATURE_PROFILES",
    "get_can_log_service",
]
