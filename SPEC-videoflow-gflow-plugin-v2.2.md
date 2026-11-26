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
| GET /vf-plugin/v1/quota | `{limits:[{media_kind,unit,accounts:[{id,label,balance,total,reset_at,status}],total_balance}]}`. Status nhận `ok, exhausted, locked, auth_required, unknown`. Dữ liệu chưa biết dùng null, gồm balance khi unknown/auth_required. |
| POST /v1/videos | JSON `{model,prompt,image,input_reference,size,seconds}`; hai field ảnh có thể mang cùng data URL của một ảnh đầu vào. Trả 202 `{id,task_id,status:"processing"}` với hai ID cùng giá trị. |
| GET /v1/videos/{id} | 200 với `status` là `processing`, `succeeded` (kèm `media_name`), `failed` (kèm error), `cancelled`, `abandoned` hoặc `reconcile_needed` (kèm error). 404 nghĩa là instance chưa tìm được ID. |
| GET /v1/media/{name} | Tên media logic do plugin cấp. Trả 200 và bytes MP4. Media đã biết nhưng hết thời hạn lưu trả 410 với kind=not_found; host giữ operation để recovery. Host dựng URL từ origin hiện tại của instance. |
| POST /v1/images/edits | Multipart gồm `image,prompt,model,n=1,response_format=b64_json`. Trả 202 với bản ghi operation khi đang chạy hoặc 200 khi thành công với `{"data":[{"b64_json":"…"}]}`. |
| DELETE /v1/videos/{id} | Trả `{status,message}`. Dùng `cancelled` khi xác nhận đã hủy trước submit, `abandoned` khi đã submit và có thể bị tính phí, giữ kết quả nếu job đã hoàn tất. Gọi lặp phải an toàn. |
| POST /vf-plugin/v1/actions/{id} | Nhận object JSON, mặc định `{}`. Trả `{status,message}`. Action video chuẩn tùy chọn `recover_media` nhận `{recoverable_media_id}`; thành công trả thêm `task_id` cho luồng poll/tải hiện có. |

**Quyền tạo lại tác vụ.** Cả hai POST tạo media bắt buộc có header `Idempotency-Key` là UUID bền vững của một thao tác. Plugin lưu key, dấu vân tay của đầu vào thực tế (gồm bytes ảnh và tùy chọn hiệu lực), trạng thái và kết quả. Cùng key khác đầu vào trả 409. Các request trùng đồng thời dùng chung một thao tác. Khi mất phản hồi, host tra UUID qua GET trên instance ban đầu. Chỉ gửi lại POST khi người dùng vẫn muốn tiếp tục, với cùng request và UUID. Sau STOP chỉ dùng DELETE và GET. Plugin chỉ được thực hiện thêm lệnh tạo ở upstream khi xác nhận lần trước chưa submit và được phép retry. Mọi kết quả chưa rõ phải giữ để đối soát. Chỉ thao tác tạo mới có chủ ý của người dùng mới cấp UUID mới.

**Lỗi.** Lỗi POST trả HTTP ngoài 2xx và body `{status:"failed",error:{code,kind,retryable,message,scope,retry_after_s?,recoverable_media_id?}}`. Poll job thất bại dùng cùng object error trong HTTP 200. Các kind là `transient,business,quota,auth,not_found,unsupported`. Core chỉ thử lại thao tác tạo khi transient và retryable=true, theo thời gian chờ nếu có. Lỗi đọc trạng thái có thể thử lại GET. Poll 404, mất kết nối, hết hạn poll hoặc job cần đối soát đều không cấp key mới. Code và message phục vụ hiển thị, message không chứa prompt, secret hay đường dẫn máy.

### Tra cứu và dừng theo UUID

| Route | Hành vi bắt buộc với cả ảnh và video |
|---|---|
| GET /vf-plugin/v1/operations/{uuid} | Chỉ đọc. Trả 404 nếu chưa có bản ghi, 200 nếu có. 404 không chứng minh POST cũ sẽ không đến. |
| DELETE /vf-plugin/v1/operations/{uuid} | Luôn hỗ trợ, kể cả khi chưa có task ID hoặc upstream không hỗ trợ hủy. Trả 200 với bản ghi operation sau khi quyết định đã được lưu. Nếu UUID chưa tồn tại, ghi dấu hủy bền vững trước khi trả lời. |

Bản ghi gồm `operation_id,status`; `media_kind` là image hoặc video, có thể thiếu khi DELETE tới trước POST. Status nhận processing, succeeded, failed, reconcile_needed, cancelled hoặc abandoned. Video đã tiếp nhận có thêm task_id; khi succeeded có thêm media_name. Ảnh succeeded chứa data[0].b64_json theo đúng định dạng của POST ảnh. failed và reconcile_needed chứa error. Khi ảnh cần đối soát mà thiếu bằng chứng hoặc file, UI giữ trạng thái cùng UUID và cho người dùng đóng việc theo dõi cục bộ. Chỉ cấp UUID khác khi người dùng chủ động chọn tạo ảnh mới.

Plugin tuần tự hóa quyết định hủy và quyết định bắt đầu submit trên cùng UUID bằng giao dịch bền vững. Nếu hủy thắng trước lúc worker nhận quyền submit, worker và mọi POST đến muộn đều bỏ qua generate. POST vào UUID đã hủy trả 409 cùng error.code=OPERATION_CANCELLED; GET vẫn trả cancelled. Nếu worker đã nhận quyền và upstream có thể đã thấy lệnh, trả abandoned khi biết đã submit, reconcile_needed khi chưa rõ. Với job terminal, DELETE trả lại trạng thái đã lưu; GET có thể trả succeeded về sau nếu tìm được media của job abandoned. Giữ bản ghi dedup và dấu hủy qua restart suốt vòng đời instance, kể cả sau khi dọn file media. Sau khi dọn file, báo kết quả cần đối soát hoặc media không còn, tránh tạo lại. DELETE video bằng task ID dùng cùng quyết định hủy này. Core lưu ý định STOP trước khi gửi DELETE, gửi lại DELETE nếu mất phản hồi hoặc instance đang offline. UI hiện “đang chờ xác nhận dừng” đến khi đọc được kết quả.

POST trùng cùng đầu vào trả cùng task ID cho video (202, sau đó GET để biết trạng thái hiện tại); ảnh đang xử lý trả 202 với operation, ảnh hoàn tất trả 200 với dữ liệu đã lưu. Nếu operation đã failed hoặc reconcile_needed và không đủ điều kiện tiếp tục, POST trả lỗi theo contract và GET trả trạng thái bền vững. Với lỗi trước submit có retryable=true, chỉ request POST chủ động tiếp tục mới có thể chuyển operation về processing. GET và DELETE không kích hoạt retry.

### Danh tính, cấu hình và phạm vi lỗi

Một instance gắn với một danh tính profile/tài khoản. Đổi profile hoặc đăng nhập tài khoản khác cần instance mới. Owner cũ và data dir cũ phải còn truy cập được để đối soát. Plugin xác định profile thực cả khi người dùng để trống tên ở lần đầu; lưu binding trong DB và kiểm tra lại khi khởi động/login. Nếu danh tính thay đổi hoặc chưa xác minh được khi resume, giữ degraded và chỉ phục vụ kết quả cục bộ hoặc đối soát an toàn. Lần lookup theo UUID đã biết đi thẳng vào DB trước khi kiểm tra readiness cho job mới. Mỗi operation lưu snapshot project, model và tùy chọn đã resolve trước submit. Khi so request trùng, dùng tham số đã gửi và snapshot gốc; các default mới chỉ áp dụng cho UUID mới.

Port ưu tiên theo thứ tự: giá trị instance `bridge_port` nếu được đặt, manifest.preferred_port, rồi port hệ điều hành cấp. Port ưu tiên bận thì host chọn port trống. Plugin bind đúng `{port}`. File `{config_file}` thuộc host và được đọc một chiều. Project do plugin tạo mặc định được lưu bền vững trong DB của instance.

Mỗi error khai `scope` là operation, media_kind hoặc instance. operation chỉ ảnh hưởng UUID hiện tại. media_kind áp dụng cho các job mới cùng loại trên instance đó; lấy loại từ request/operation. instance áp dụng cho mọi job tạo mới ở instance đó. GET, recovery và các instance khác vẫn hoạt động. retryable quyết định có được thử lại việc tạo cho UUID đó; retry_after_s độc lập, là thời gian tối thiểu tính từ khi nhận phản hồi cho phạm vi bị chặn. Khi thiếu thời hạn cho lỗi quota/auth diện rộng, chỉ mở lại sau lần kiểm tra quota/health thành công xác nhận điều kiện đã hết. Quota trả từng nhóm limits theo media_kind và unit. Không áp số dư từ nhóm video cho ảnh.

### Quy ước HTTP dùng cho hai phía

Xác thực trước khi xử lý mọi route, kể cả GET /healthz. Bearer phiên thiếu hoặc sai trả 401. Khi phiên Google hết hạn, Bearer giữa host và plugin vẫn hợp lệ. Ánh xạ dưới đây áp dụng khi request thất bại ngay tại HTTP. GET đọc được một job đã failed vẫn trả 200 với lỗi của job. Health dùng mã theo trạng thái tổng hợp như bảng route.

| Điều kiện | HTTP | kind trong error |
|---|---:|---|
| Sai Bearer phiên | 401 | auth, code PLUGIN_TOKEN_INVALID |
| Cần đăng nhập lại upstream | 424 | auth |
| Thiếu quota upstream | 429 | quota |
| Rate limit có xác nhận | 429 | transient |
| Upstream tạm mất kết nối, cooldown hoặc worker bận | 503 | transient |
| JSON/multipart sai cấu trúc, kiểu, trường xung đột | 400 | business |
| Dữ liệu đúng kiểu nhưng ngoài model/duration/aspect hoặc vượt cap | 422 | business |
| Plugin nhận diện tính năng nhưng chưa hỗ trợ | 501 | unsupported |
| ID tài nguyên/action không tồn tại, kể cả DELETE video bằng task ID | 404 | not_found |
| UUID trùng khác payload hoặc POST đến sau khi UUID đã hủy | 409 | business |

Host dùng kind, scope và retryable để quyết định xử lý. Với 401 host kiểm tra phiên process; với 424 host hiển thị action login. DELETE theo UUID **chưa tồn tại** vẫn ghi dấu hủy và trả 200 như mục tra cứu theo UUID.

**Health.** checks là object có khóa là tên phép kiểm tra ổn định và giá trị là object có status (ok, degraded hoặc down) và message dạng string. Host chấp nhận tên phép kiểm tra mới. Status cấp ngoài do plugin tổng hợp theo khả năng nhận job. Khi process không trả HTTP, host ghi nhận down cục bộ.

```json
{"status":"degraded","checks":{"http":{"status":"ok","message":"Sẵn sàng"},"account":{"status":"degraded","message":"Cần đăng nhập lại"}},"message":"Tạm dừng nhận job mới"}
```

**Video.** size là chuỗi kích thước pixel gồm hai số nguyên dương nối bằng chữ x thường, ví dụ "1280x720". seconds là số nguyên JSON dương. Chuỗi "16:9" cho size hoặc "8" cho seconds trả 400. Với i2v, gửi ít nhất một trong image và input_reference dưới dạng data URL ảnh base64. Khi có cả hai, giải mã và so bytes; khác bytes hoặc data URL hỏng trả 400 trước submit. Tỷ lệ sau rút gọn và thời lượng phải được model hỗ trợ.

**Ảnh.** Multipart có thể thêm aspect_ratio dạng chuỗi như "9:16", thuộc aspects của model. Bỏ trống dùng mặc định của plugin và lưu giá trị hiệu lực cùng operation. Không nhận field size ở route ảnh (400). Check fingerprint gồm aspect hiệu lực. Khi bytes kết quả đã lưu sẵn lúc trả lời, gửi 200 với data; khi đã tiếp nhận nhưng chưa có kết quả, gửi 202 với operation processing, kể cả POST đầu tiên hoặc bản gửi trùng. Sau 202 host dùng GET theo UUID.

**Actions.** Mỗi phần tử manifest.actions có id, label, scope (instance hoặc job, mặc định instance) và confirm tùy chọn. login và open_project nhận {}, scope=instance. Trả 202 với {"status":"started","message":"Đã mở trình duyệt"} ngay sau khi mở thành công. Đây là xác nhận đã mở, host đọc lại health để biết kết quả đăng nhập. Lỗi mở trình duyệt trả error tại HTTP. recover_media dùng scope=job; host chỉ hiện trên job của instance có recoverable_media_id. Khi đã nhận và có task để poll, trả 202 với status=started, message, task_id; nếu phục hồi đã xong, trả 200 với status=succeeded, message, task_id. Sau cả hai host poll task_id trên đúng instance.

**Config và manifest.** Tên file là manifest.json ở gốc gói. min_core_version là chuỗi SemVer bắt buộc trong gói phát hành, bằng release VideoFlow sớm nhất đã vượt suite Contract v1. Trong manifest dev/fixture, giá trị null nghĩa là chưa chốt release. Host so SemVer khi có giá trị. Host cung cấp một snapshot config cho mỗi lần spawn. Plugin đọc một lần khi khởi động. Lưu thay đổi config hoặc secret đánh dấu restart_required; host ngừng cấp việc mới, cho việc đang xử lý đến điểm an toàn, rồi spawn phiên mới với config đã ghi nguyên tử và secret mới. Nếu vẫn còn lượt upstream phụ thuộc process đang chạy thì hoãn restart. Job resume dùng snapshot của operation ban đầu.

**Kiểm thử giao diện.** Kiểm tra riêng 401 của health, 424 của POST khi upstream mất phiên, 429 quota và 429 transient, cả hai dạng DELETE ID chưa biết, ảnh trùng còn chạy → 202 và đã lưu → 200, hai ảnh video khác bytes → 400 với 0 submit. Thay config trong lúc job đang chạy phải giữ snapshot cũ đến khi job kết thúc.

### Fixture tương thích v1.2 (Contract major 1)

Hai repo có thể trích trực tiếp các JSON dưới đây. Fixture đầu là một manifest đầy đủ cho process giả; CI cung cấp executable có tên trong fixture. Fixture sau chứa các response theo tên ca test; mỗi giá trị trong object là một body HTTP riêng. Các key bao ngoài chỉ dùng để đặt tên test.

```json
{"id":"demo","min_core_version":null,"name":"Demo","version":"0.1.0","plugin_api_version":"1","entry":{"exe":"demo.exe","args":["--port","{port}","--data-dir","{data_dir}","--config-file","{config_file}"]},"preferred_port":18611,"health_path":"/healthz","capabilities_path":"/vf-plugin/v1/capabilities","quota_path":"/vf-plugin/v1/quota","config_schema":{"type":"object","properties":{"video_model":{"type":"string","default":"m1","x-dynamic-enum":"capabilities.video.models[].id"}}},"actions":[]}
```

```json
{"capabilities":{"plugin_api_version":"1","kinds":["video","image"],"stale":false,"video":{"modes":["i2v"],"models":[{"id":"m1","label":"Demo video","aspects":["16:9"],"durations":[8]}],"max_concurrent_jobs_per_account":1,"poll_hint":{"interval_s":15,"max_s":1800},"supports_cancel":"before_submit_only"},"image":{"modes":["i2i"],"models":[{"id":"i1","label":"Demo image","aspects":["1:1"]}]}},"quota":{"limits":[{"media_kind":"video","unit":"credits","accounts":[{"id":"default","label":"A","balance":0,"total":100,"reset_at":null,"status":"exhausted"}],"total_balance":0}]},"video_post_replay_202":{"id":"t1","task_id":"t1","status":"processing"},"video_poll_200":{"status":"succeeded","media_name":"clip.mp4"},"image_pending_202":{"operation_id":"123e4567-e89b-42d3-a456-426614174000","media_kind":"image","status":"processing"},"image_lookup_200":{"operation_id":"123e4567-e89b-42d3-a456-426614174000","media_kind":"image","status":"succeeded","data":[{"b64_json":"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg=="}]},"cancel_before_post_200":{"operation_id":"123e4567-e89b-42d3-a456-426614174001","status":"cancelled"},"cooldown":{"status":"failed","error":{"kind":"transient","code":"PROVIDER_RATE_LIMITED","scope":"instance","retryable":false,"retry_after_s":1800,"message":"Tạm dừng nhận việc trên instance này."}}}
```

Test bắt buộc trên cùng fixture: DELETE trước POST và restart rồi POST muộn → 0 generate; POST đã submit trước DELETE → tối đa 1 generate; hai POST ảnh đồng thời rồi mất phản hồi → GET trả ảnh đã lưu, tổng cộng 1 generate. Đổi model/project sau submit vẫn lấy đúng kết quả gốc. Đổi profile tạo instance mới, B không nhận job cũ của A. Cooldown instance A vẫn cho B chạy; quota video cạn vẫn cho tạo ảnh nếu hạn mức ảnh cho phép.

## Manifest của plugin

```json
{
  "id": "gflow",
  "min_core_version": null,
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
        "description": "Để trống = plugin tạo 1 project chung rồi lưu ID vào jobs.db của instance."
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
        "description": "Port ưa thích của instance này; bận thì host tự xin port khác."
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
      "scope": "instance",
      "label": "Đăng nhập Flow",
      "confirm": "Mở Chrome để đăng nhập Google Flow?"
    },
    {
      "id": "open_project",
      "scope": "instance",
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

Trong jobs.db đặt UNIQUE trên operation UUID. Ghi giao dịch đặt chỗ với bridge task ID và fingerprint trước khi gửi. Ghi trạng thái sắp submit trước side effect, ghi upstream task ID ngay khi nhận. Gọi generate với wait=false; poll tasks/get bằng ID upstream. Trạng thái completed phải kiểm tra result.isError. Phân biệt lỗi gọi GET với lỗi của job. Khi process chết, đọc lại các hàng dang dở. Nếu có ID, tra lại trên cùng profile; nếu thiếu ID sau thời điểm có thể submit, giữ reconcile_needed. Job processing bỏ lại sau khi upstream chết phải có giới hạn chờ và đường đối soát. Khi tìm được media đã tạo, tải lại bằng media ID và liên kết với operation cũ. Với ảnh đồng bộ, lưu kết quả trước khi gửi response để replay trả đúng ảnh. STOP ghi quyết định hủy theo UUID trước; khi đã có upstream task ID phù hợp thì gọi tasks/cancel và tra lại kết quả. Lỗi vượt cap dùng scope=operation, quota video dùng scope=media_kind, WAF dùng scope=instance.

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

## Stack, đóng gói và lưu trữ mặc định

Dùng CPython 3.12 x64 trên Windows, FastAPI, Uvicorn một worker mỗi instance, python-multipart cho upload, sqlite3 và pytest. Dùng Python SDK chính thức mcp cho stdio và Streamable HTTP; khóa phiên bản SDK và các dependency đã qua test Tasks với gflow-cli 0.107.0 trong lockfile của repo plugin. Build PyInstaller ở chế độ onedir trên Windows. Gói release chứa manifest.json, videoflow-gflow.exe và toàn bộ thư mục phụ thuộc do onedir sinh ra. MVP dùng executable gflow đã cài riêng trên PATH. Smoke test gói trên máy Windows sạch có gflow và Chrome: spawn bằng manifest, xác thực health, đọc catalog, login đúng home và chạy fixture. CI release từ chối min_core_version null hoặc bản core chưa phát hành/chưa qua suite.

**Job bị bỏ lại.** Mốc mặc định là poll_hint.max_s=1800 giây kể từ lần cuối có bằng chứng upstream tiến triển, hoặc từ khi submit nếu chưa có tiến triển. Lưu timestamp trong SQLite; poll lặp cùng trạng thái và restart không đặt lại đồng hồ. Khi mất worker/upstream, tìm lại đúng upstream ID và kiểm tra kết quả trước. Hết mốc mà chưa xác định được kết quả thì ghi reconcile_needed, giữ UUID, media ID và dấu vết đối soát. Hết thời gian chờ ở host chỉ kết thúc lượt chờ của UI.

**Media.** Ngân sách mềm cho MP4 là 10 GiB mỗi instance. Giữ tối thiểu 7 ngày kể từ lần hoàn tất hoặc tải thành công gần nhất (lấy mốc muộn hơn). Quét khi startup và hằng ngày; sau 30 ngày không truy cập có thể xóa MP4 đủ điều kiện, hoặc khi vượt ngân sách thì xóa file đủ 7 ngày theo lần truy cập cũ nhất đến khi về dưới ngưỡng. Bỏ qua job processing/reconcile_needed, file đang tải hoặc đang được khôi phục. Nếu không đủ file được phép dọn, giữ file, báo dung lượng và từ chối job mới khi không ghi được an toàn. Sau dọn, GET media đã biết trả 410 với kind=not_found, lookup operation chuyển reconcile_needed nếu cần khôi phục; giữ dedup, dấu hủy và recovery ID. Host hiển thị cách lấy lại clip hoặc kết quả đã hết hạn. Kiểm thử bằng đồng hồ giả tại các mốc 7/30 ngày, tải song song và restart qua mốc 1800 giây.

## Mốc bàn giao và nghiệm thu

1. **Spike và video một profile.** Dùng đúng home để đăng nhập. Ghi nhận model, thời gian, lượng credits và file MP4 của một clip i2v khi có tài khoản thử. Đóng gói bản dev với CLI được cài riêng.
2. **Test độc lập bằng pytest, host và MCP giả.** Gồm đổi tên tool rồi thêm alias, tham số bắt buộc lạ, model mới, lỗi chưa biết, hai POST đồng thời cùng key, 409 khác payload, mất response và crash trước/sau lưu upstream ID. Đếm số lần thực gọi generate. Kiểm tra file confinement, MP4 hỏng, Bearer sai, stale lock khi Chrome chết, thiếu auth và cooldown.
3. **Ảnh, recovery và đa profile.** Chạy B2 mất response sau thành công, lấy lại clip đã tính tiền, hủy trước/sau submit, hai process hai profile và 10 dòng. Tắt một instance và phục hồi qua port mới. Test transport HTTP riêng. Phát hành gói Windows cùng manifest, phiên bản CLI đã thử, yêu cầu cài đặt và kết quả suite Contract v1.
4. **Mở rộng.** Extend, merge, upscale và character chỉ công bố khi có giao diện gọi và test đầu cuối tương ứng; nếu contract hiện hành chưa diễn tả được thao tác, đề xuất phiên bản hợp đồng trước khi quảng bá.

Tài liệu upstream theo commit tham chiếu: [MCP và vòng đời task](https://github.com/ffroliva/gflow-cli/blob/2fcb824/docs/MCP.md), [phân loại lỗi](https://github.com/ffroliva/gflow-cli/blob/2fcb824/src/gflow_cli/errors.py), [cấu hình profile và output](https://github.com/ffroliva/gflow-cli/blob/2fcb824/docs/CONFIGURATION.md).
