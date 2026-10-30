"""Windows 上管住实时核子进程的 Job Object 与挂起启动。

设计见 `docs/modules/realtime-core.md` 的“启动与生命线”：

- 子进程以 `CREATE_SUSPENDED` 创建，放进 job、交出 job 句柄之后才恢复运行：
  它在进 job 之前做不了任何事，它拉起的孙进程也一定在 job 里；
- job 设 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`：最后一个 job 句柄关闭时，
  系统终止 job 里剩下的进程；
- 主进程把 job 句柄复制一份到子进程里，子进程不知道也不使用它，只是持有它。
  主进程崩溃时系统关闭它的句柄，但子进程持有的那份让 job 不关闭，实时核不会被立即杀掉，
  能按生命线释放输入再退出；实时核退出后最后一个句柄关闭，job 收拾残留的孙进程。
  复制过去的句柄不可继承，孙进程不持有它。
"""

import sys

if sys.platform != "win32":
    raise ImportError("_win32 只用于 Windows")

import ctypes
from ctypes import wintypes
from typing import Any

CREATE_SUSPENDED = 0x00000004

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_DUP_HANDLE = 0x0040
_PROCESS_SET_QUOTA = 0x0100
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_RESUME_FAILED = 0xFFFFFFFF

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _function(name: str, restype: Any, *argtypes: Any) -> Any:
    function = getattr(_kernel32, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = (
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    )


class _ThreadEntry32(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    )


_GetCurrentProcess = _function("GetCurrentProcess", wintypes.HANDLE)
_CloseHandle = _function("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_CreateJobObjectW = _function(
    "CreateJobObjectW", wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCWSTR
)
_SetInformationJobObject = _function(
    "SetInformationJobObject",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
)
_QueryInformationJobObject = _function(
    "QueryInformationJobObject",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
)
_AssignProcessToJobObject = _function(
    "AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE
)
_TerminateJobObject = _function("TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
_OpenProcess = _function(
    "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
)
_DuplicateHandle = _function(
    "DuplicateHandle",
    wintypes.BOOL,
    wintypes.HANDLE,
    wintypes.HANDLE,
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
)
_CreateToolhelp32Snapshot = _function(
    "CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD
)
_Thread32First = _function(
    "Thread32First", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)
)
_Thread32Next = _function(
    "Thread32Next", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)
)
_OpenThread = _function(
    "OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
)
_ResumeThread = _function("ResumeThread", wintypes.DWORD, wintypes.HANDLE)


def _check(ok: object) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def _open_process(pid: int, access: int) -> Any:
    handle = _OpenProcess(access, False, pid)
    _check(handle)
    return handle


class JobObject:
    """一个匿名的 Job Object，设了 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`。"""

    def __init__(self) -> None:
        handle = _CreateJobObjectW(None, None)
        _check(handle)
        self._handle: Any = handle
        try:
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(
                _SetInformationJobObject(
                    handle,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
            )
        except OSError:
            self.close()
            raise

    def adopt(self, pid: int) -> None:
        """把进程放进 job，并把 job 句柄复制一份到该进程里，由它持有到退出。"""
        job = self._open_handle()
        process = _open_process(pid, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_DUP_HANDLE)
        try:
            _check(_AssignProcessToJobObject(job, process))
            # 复制出的句柄只为让 job 活到子进程退出：只给查询权限，不可继承；
            # 子进程不需要知道它的值。
            duplicated = wintypes.HANDLE()
            _check(
                _DuplicateHandle(
                    _GetCurrentProcess(),
                    job,
                    process,
                    ctypes.byref(duplicated),
                    _JOB_OBJECT_QUERY,
                    False,
                    0,
                )
            )
        finally:
            _CloseHandle(process)

    def _open_handle(self) -> Any:
        # 句柄为空时 Query/TerminateJobObject 会作用于调用进程自己所在的 job，必须拦住。
        if self._handle is None:
            raise OSError("job 句柄已关闭")
        return self._handle

    def active_processes(self) -> int:
        """job 里还在运行的进程数。"""
        info = _BasicAccountingInformation()
        _check(
            _QueryInformationJobObject(
                self._open_handle(),
                _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            )
        )
        return int(info.ActiveProcesses)

    def terminate(self, exit_code: int) -> None:
        """终止 job 里的全部进程。终止是异步的，用 `active_processes` 确认。"""
        _check(_TerminateJobObject(self._open_handle(), exit_code))

    def close(self) -> None:
        """关闭本进程持有的 job 句柄；可以重复调用。"""
        if self._handle is not None:
            _CloseHandle(self._handle)
            self._handle = None


def resume_suspended(pid: int) -> None:
    """恢复以 `CREATE_SUSPENDED` 创建、尚未运行的进程。

    这样的进程只有一个线程（初始线程），在系统的线程快照里按进程号找到它再恢复。
    """
    snapshot = _CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    threads: list[int] = []
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        more = _Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                threads.append(int(entry.th32ThreadID))
            more = _Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        _CloseHandle(snapshot)
    if len(threads) != 1:
        raise OSError(f"挂起的进程 {pid} 应恰有 1 个线程，找到 {len(threads)} 个")
    thread = _OpenThread(_THREAD_SUSPEND_RESUME, False, threads[0])
    _check(thread)
    try:
        if _ResumeThread(thread) == _RESUME_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _CloseHandle(thread)
