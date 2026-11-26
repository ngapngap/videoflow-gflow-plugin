# videoflow-gflow-plugin

Plugin cầu nối giữa **VideoFlow** (Contract v1) và **Google Flow (`gflow-cli >= 0.107.0`)** trên Windows (CPython 3.12 x64).

## Yêu cầu cài đặt
- Windows x64, Python 3.12 (cho phát triển/build)
- [`gflow-cli >= 0.107.0`](https://github.com/ffroliva/gflow-cli) có sẵn trên `PATH` và Google Chrome
- `uv` để quản lý môi trường và lockfile

## Chạy kiểm thử độc lập (không cần tài khoản Google Flow)
```powershell
uv sync --frozen --group dev
uv run pytest -v
```

## Đóng gói Windows (`onedir`)
- Bản dev (`min_core_version: null`):
```powershell
uv run python build_tools/build_release.py --dev
```
- Bản release (bắt buộc truyền `min_core_version` đã qua suite Contract v1):
```powershell
uv run python build_tools/build_release.py --release --min-core-version 1.5.5 --released-cores 1.5.5
```
Thư mục đầu ra nằm tại `dist/videoflow-gflow/` gồm `videoflow-gflow.exe`, `_internal/` và `manifest.json`.
