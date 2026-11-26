# Đặc tả triển khai plugin gflow cho VideoFlow · v2.2

**Repo đích:** `ngapngap/videoflow-gflow-plugin`. Dự thảo triển khai Windows, dựa trên gflow-cli 0.107.0 và thiết kế VideoFlow v1.5.5. Repo này phát hành plugin độc lập. Đầu ra là thư mục cài đặt gồm manifest và executable. Có thể phát triển với host giả, không cần checkout VideoFlow.

## Plugin Contract v1

Hai repo dùng cùng phiên bản hợp đồng này. Repo VideoFlow quản lý bộ fixture và test tương thích có version để CI của plugin chạy độc lập. Thay đổi phá tương thích cần major mới.

### Khởi động một instance

Manifest khai `id`, `name`, `version`, `plugin_api_version: "1"`, `entry.exe`, `entry.args`, `preferred_port`, `health_path`, `capabilities_path`, `quota_path`, `config_schema` và `actions[]`. ID plugin và instance khớp `^[a-z0-9-]+$`. Phiên bản core tối thiểu trong gói phát hành phải là số release thật. Host thay `{port}`, `{data_dir}`, `{config_file}` trong từng đối số. Config file là JSON của instance, chỉ chứa giá trị công khai. Host truyền Bearer ngẫu nhiên qua biến môi trường `VF_PLUGIN_TOKEN` và object JSON các field bí mật qua `VF_PLUGIN_SECRETS_JSON`. Plugin chỉ bind 127.0.0.1. Mọi route kiểm tra Bearer, trả 401 khi sai. Host chỉ gửi key tới đúng origin của instance. Không ghi token hay secret vào log.

### HTTP và dữ liệu

| Route | Yêu cầu và kết quả |
|---|---|
| GET /healthz | JSON `{status,checks,message}`. HTTP 200 cho `ok`, 503 cho `degraded` hoặc `down`. Khi degraded, dừng cấp job mới và vẫn cho poll, tải, đăng nhập hoặc đối soát. |
| GET /vf-plugin/v1/capabilities | `plugin_api_version,kinds,stale`. Mỗi kind công bố `modes,models`; mỗi model có `id,label,aspects`, video có `durations` tính bằng giây, tùy chọn `retires_on,cost_hint`. Video khai `max_concurrent_jobs_per_account,poll_hint:{interval_s,max_s},supports_cancel` (`none` hoặc `before_submit_only`). Chỉ quảng bá tính năng đã triển khai. |
| GET /vf-plugin/v1/quota | `{unit,accounts:[{id,label,balance,total,reset_at,status}],total_balance}`. Status nhận `ok, exhausted, locked, auth_required, unknown`. Dữ liệu chưa biết dùng null, gồm balance khi unknown/auth_required. |
| POST /v1/videos | JSON `{model,prompt,image,input_reference,size,seconds}`; hai field ảnh có thể mang cùng data URL của một ảnh đầu vào. Trả 202 `{id,task_id,status:"processing"}` với hai ID cùng giá trị. |
| GET /v1/videos/{id} | 200 với `status` là `processing`, `succeeded` (kèm `media_name`), `failed` (kèm error), `cancelled`, `abandoned` hoặc `reconcile_needed` (kèm error). 404 nghĩa là instance chưa tìm được ID. |
| GET /v1/media/{name} | Tên media logic do plugin cấp. Trả 200 và bytes MP4. Host dựng URL từ origin hiện tại của instance. |
| POST /v1/images/edits | Multipart gồm `image,prompt,model,n=1,response_format=b64_json`. Thành công 200 với `{"data":[{"b64_json":"…"}]}`. |
| DELETE /v1/videos/{id} | Trả `{status,message}`. Dùng `cancelled` khi xác nhận đã hủy trước submit, `abandoned` khi đã submit và có thể bị tính phí, giữ kết quả nếu job đã hoàn tất. Gọi lặp phải an toàn. |
| POST /vf-plugin/v1/actions/{id} | Nhận object JSON, mặc định `{}`. Trả `{status,message}`. Action chuẩn tùy chọn `recover_media` nhận `{recoverable_media_id}`; thành công trả thêm `task_id` cho luồng poll/tải hiện có. |

**Quyền tạo lại tác vụ.** Cả hai POST tạo media bắt buộc có header `Idempotency-Key` là UUID bền vững của một thao tác. Plugin lưu key, dấu vân tay của đầu vào thực tế (gồm bytes ảnh và tùy chọn hiệu lực), trạng thái và kết quả. Cùng key khác đầu vào trả 409. Các request trùng đồng thời dùng chung một thao tác. Khi mất phản hồi, host có thể gửi lại đúng request, key và instance để tra lại kết quả. Plugin chỉ được thực hiện thêm lệnh tạo ở upstream khi xác nhận lần trước chưa submit và được phép retry. Mọi kết quả chưa rõ phải giữ để đối soát. Chỉ thao tác tạo mới có chủ ý của người dùng mới cấp UUID mới.

**Lỗi.** Lỗi POST trả HTTP ngoài 2xx và body `{status:"failed",error:{code,kind,retryable,message,retry_after_s?,recoverable_media_id?}}`. Poll job thất bại dùng cùng object error trong HTTP 200. Các kind là `transient,business,quota,auth,not_found,unsupported`. Core chỉ thử lại thao tác tạo khi transient và retryable=true, theo thời gian chờ nếu có. Lỗi đọc trạng thái có thể thử lại GET. Poll 404, mất kết nối, hết hạn poll hoặc job cần đối soát đều không cấp key mới. Code và message phục vụ hiển thị, message không chứa prompt, secret hay đường dẫn máy.

## Manifest của plugin

```json
{
  "id": "gflow",
  "name": "Google Flow (gflow-cli)",
  "version": "0.1.0",
  "plugin_api_version": "1",
  "entry": {
    "exe": "videoflow-gflow.exe",
    "args": [
      "--port",
      "{port}",
      "--data-dir",
      "{data_dir}",
      "--config-file",
      "{config_file}"
    ]
  },
  "data_dir_name": "gflow",
  "preferred_port": 18611,
  "health_path": "/healthz",
  "capabilities_path": "/vf-plugin/v1/capabilities",
  "quota_path": "/vf-plugin/v1/quota",
  "upstream": {
    "name": "gflow-cli",
    "min_version": "0.107.0"
  },
  "permissions": [
    "network:loopback",
    "browser:chrome",
    "fs:own-data-dir"
  ],
  "config_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
      "video_model"
    ],
    "properties": {
      "video_model": {
        "type": "string",
        "title": "Model video",
        "default": "veo-fast",
        "x-dynamic-enum": "capabilities.video.models[].id",
        "description": "Giá trị phải nằm trong capabilities.video.models[].id; plugin tự validate lại lúc submit."
      },
      "image_model": {
        "type": "string",
        "title": "Model ảnh",
        "default": "nano2",
        "x-dynamic-enum": "capabilities.image.models[].id"
      },
      "profile": {
        "type": "string",
        "title": "Profile Flow",
        "default": "",
        "description": "Để trống = plugin tự resolve như gflow (env GFLOW_CLI_PROFILE → config.toml default_profile → profile duy nhất). Tên trơn, không chứa dấu phân cách đường dẫn."
      },
      "project_id": {
        "type": "string",
        "title": "Flow project id",
        "default": "",
        "description": "Để trống = plugin tạo 1 project chung rồi lưu lại vào config."
      },
      "max_credits_per_clip": {
        "type": "integer",
        "title": "Cap credits / clip",
        "default": 10,
        "minimum": 0,
        "maximum": 100
      },
      "bridge_port": {
        "type": "integer",
        "title": "Port plugin",
        "default": 18611,
        "minimum": 1,
        "maximum": 65535,
        "description": "Port ưa thích của instance này; bận thì host tự xin port khác (xem §2.6)."
      },
      "proxy": {
        "type": "string",
        "title": "Proxy của profile",
        "default": "",
        "x-secret": true,
        "description": "Để trống = không dùng proxy. Chỉ dùng proxy riêng của profile, không dùng proxy share nhiều người."
      },
      "daemon_token": {
        "type": "string",
        "title": "Daemon token",
        "default": "",
        "x-secret": true,
        "description": "Chỉ dùng khi transport HTTP (gflow serve). Lưu vào Credential Manager, không lưu vào plugins.json."
      }
    }
  },
  "actions": [
    {
      "id": "login",
      "label": "Đăng nhập Flow",
      "confirm": "Mở Chrome để đăng nhập Google Flow?"
    },
    {
      "id": "open_project",
      "label": "Mở Flow trên web"
    }
  ]
}
```

Ví dụ trên dành cho phát triển. Khi đóng gói, đặt min_core_version theo bản core thực tế đã qua test. Khi có hỗ trợ phục hồi, thêm action `recover_media` kiểu theo ngữ cảnh job. Chỉ bật các kind/action đã chạy được trong phase tương ứng.

## Thành phần cần triển khai

- `plugin.py` phục vụ HTTP, xác thực Bearer, quản lý job SQLite và file media. Mỗi instance một process, một profile, một thư mục dữ liệu. Đặt `GFLOW_CLI_HOME` và `GFLOW_CLI_OUTPUT_DIR` bên trong data dir, login bằng chính môi trường và profile đó ngay từ lần chạy đầu. Đọc proxy và daemon token từ secret bootstrap, loại khỏi môi trường truyền xuống process không cần chúng.
- `mcp_client.py` quản lý stdio `gflow mcp run`; phase sau thêm Streamable HTTP tại `/mcp`. Trước khi nhận job, kiểm tra min version, đọc tools/list, chọn alias theo bảng tập trung, so schema gồm kiểu và tham số bắt buộc với dữ liệu plugin có thể cung cấp. Tool mới vượt khả năng xử lý trả lỗi tương thích có hướng dẫn. Upstream mới hơn đã kiểm thử chỉ cảnh báo nếu self-test đạt.
- Bảng alias ban đầu gồm generate_video → gflow_generate_video, list_models → gflow_list_models, get_credits → gflow_get_credits. Ánh xạ generate_image, download_media, extend_video, upscale_video, character_list sang tên gflow tương ứng khi hỗ trợ. Thiếu tool phụ thì bỏ capability phụ thuộc. Kiểm tra hỗ trợ Tasks cho đường video async.
- `gflow_models.py` lấy catalog runtime, dịch alias, duration, aspect và thông tin retire. Khi upstream tạm mất, chỉ dùng cache với stale=true. Validate lại model và duration ở lúc submit. Dịch size sang aspect theo kích thước; từ chối tỷ lệ không hỗ trợ. Giải mã ảnh vào file tạm riêng theo operation. Chỉ nhận media trong output root, kiểm tra MP4 trước succeeded, giữ media đủ cho resume. Dọn file tạm sau khi trạng thái đã bền vững.
- Giữ một lượt tạo tại một thời điểm trên profile kể cả khi B2 và B3 đến đồng thời. Resolve project theo config, nếu cần tạo thì lưu lại ID tránh tạo trùng. Truyền max_credits cho upstream. Theo dõi MCP và khả năng dùng account riêng biệt. File lease vẫn tồn tại sau khi nhả khóa; số dư cache cũng không chứng minh Chrome còn hoạt động. Health cần phát hiện browser đang xử lý bị mất và phân biệt với profile rảnh chưa mở Chrome.

## Nhật ký submit và khôi phục

Trong jobs.db đặt UNIQUE trên operation UUID. Ghi giao dịch đặt chỗ với bridge task ID và fingerprint trước khi gửi. Ghi trạng thái sắp submit trước side effect, ghi upstream task ID ngay khi nhận. Gọi generate với wait=false; poll tasks/get bằng ID upstream. Trạng thái completed phải kiểm tra result.isError. Phân biệt lỗi gọi GET với lỗi của job. Khi process chết, đọc lại các hàng dang dở. Nếu có ID, tra lại trên cùng profile; nếu thiếu ID sau thời điểm có thể submit, giữ reconcile_needed. Job processing bỏ lại sau khi upstream chết phải có giới hạn chờ và đường đối soát. Khi tìm được media đã tạo, tải lại bằng media ID và liên kết với operation cũ. Với ảnh đồng bộ, lưu kết quả trước khi gửi response để replay trả đúng ảnh. STOP dùng tasks/cancel và tra trạng thái thực trước khi báo kết quả.

## Dịch lỗi và hạn mức

`gflow_errors.py` ưu tiên type, charged, retryable, next_action và operation/media ID trong từng envelope. Chỉ cho phép tạo lại khi charged=no và bằng chứng xác nhận thao tác an toàn. Với exit code đơn lẻ không phân biệt được giai đoạn, chọn reconcile_needed khi có khả năng đã submit. Timeout sau submit và lỗi tải clip đã thanh toán đều giữ job. Dùng recoverable_media_id khi upstream đã xác định media. Áp dụng bảng sau cùng quy tắc trên.

| Tình huống | Kết quả |
|---|---|
| Rate limit, network, timeout, browser đóng | transient. Giá trị retryable theo kết quả cụ thể. |
| WAF | transient, retryable=false, cooldown theo instance; mốc mặc định 1800 giây. |
| Tài khoản thiếu credits | quota; báo số dư khi biết. |
| Giá clip vượt cap | business, retryable=false; báo giá và cap để người dùng sửa. |
| Auth thiếu/hết hạn, profile bị giữ | auth kèm hướng dẫn login; với profile bận đã xác định trước submit trả transient kèm thời gian chờ. |
| Content policy, UI/schema thay đổi, media không xác định, lỗi lạ | business hoặc unsupported theo nguyên nhân; giữ trạng thái cần đối soát nếu chưa biết kết quả. |

Quota credits của video và hạn mức ảnh hằng ngày có đơn vị khác nhau. Không suy ra hết quota ảnh từ số dư video. Lọc log và mọi lỗi trước khi trả ra ngoài.

## Mốc bàn giao và nghiệm thu

1. **Spike và video một profile.** Dùng đúng home để đăng nhập. Ghi nhận model, thời gian, lượng credits và file MP4 của một clip i2v khi có tài khoản thử. Đóng gói bản dev với CLI được cài riêng.
2. **Test độc lập bằng pytest, host và MCP giả.** Gồm đổi tên tool rồi thêm alias, tham số bắt buộc lạ, model mới, lỗi chưa biết, hai POST đồng thời cùng key, 409 khác payload, mất response và crash trước/sau lưu upstream ID. Đếm số lần thực gọi generate. Kiểm tra file confinement, MP4 hỏng, Bearer sai, stale lock khi Chrome chết, thiếu auth và cooldown.
3. **Ảnh, recovery và đa profile.** Chạy B2 mất response sau thành công, lấy lại clip đã tính tiền, hủy trước/sau submit, hai process hai profile và 10 dòng. Tắt một instance và phục hồi qua port mới. Test transport HTTP riêng. Phát hành gói Windows cùng manifest, phiên bản CLI đã thử, yêu cầu cài đặt và kết quả suite Contract v1.
4. **Mở rộng.** Extend, merge, upscale và character chỉ công bố khi có giao diện gọi và test đầu cuối tương ứng; nếu contract hiện hành chưa diễn tả được thao tác, đề xuất phiên bản hợp đồng trước khi quảng bá.

Tài liệu upstream theo commit tham chiếu: [MCP và vòng đời task](https://github.com/ffroliva/gflow-cli/blob/2fcb824/docs/MCP.md), [phân loại lỗi](https://github.com/ffroliva/gflow-cli/blob/2fcb824/src/gflow_cli/errors.py), [cấu hình profile và output](https://github.com/ffroliva/gflow-cli/blob/2fcb824/docs/CONFIGURATION.md).