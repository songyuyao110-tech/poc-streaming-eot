启动：`/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -u -B /Users/YUYSONG/Work/poc-streaming-eot/ui/server.py`；打开 http://127.0.0.1:8765/，刷新页面后输入 query，Enter/发送，POST `/api/run/stream` 阶段 SSE 即时更新时间线（执行仅 mock 展示，旧 `/api/run` 保留）；停止：前台 Ctrl+C，后台 `kill $(lsof -t -iTCP:8765 -sTCP:LISTEN)`。

内心戏页（独立页，2026-11-11 新增；**2026-11-17 起是现行面向用户的界面、也是最终验收用的那一页**）：http://127.0.0.1:8765/neixinxi （同进程，`GET /neixinxi`）。铺满一屏的**彩色相机画面**（未开画面时纯色兜底）上，顶栏（标题 · 主驾/副驾选麦 · 两路音量门槛 · 画面状态）、左右两路字幕（主驾青绿 / 副驾琥珀，字幕上带「已说完 / 正在说 / 没说完」）、字幕之上**中下部的读心旁白叠字**（大字号 + 深描边 + 浅色填充 + 轻微彩色残影，每次随机大小/位置/角度，旧句原地褪成半透明最多叠 4 层，新句放大弹入 + 短促一抖）。链路（2026-11-17 现行）：双路 mic → 双路流式 ASR → 视频接入 → 两路音量门槛（没过线这一路不送识别）→ 判停接到两路识别（只判「说完没说完」，标可不可确认）→ 同一拍问「要不要回答 / 应声 / 节奏 / 垫句」→ 读心旁白；这句收住后进主链正式答复，办完把对人说的那一句回到中间大字。接口 `POST /api/neixinxi/open`、`WS /api/neixinxi/asr?session_id=&seat=D|P`、`WS /api/neixinxi/text?session_id=&seat=D|P`、`POST /api/neixinxi/reset`；`?demo=1` 只放演示旁白（不连服务，验版式用）；**不带参数打开＝真判断**，`?eot=offline` 才切离线替身（离线通过只算预检，不算用户界面最终验收）；工程测试台 `/` 保持原样、只作调试，不是验收面。

**这一页的现行口径（2026-11-17）**：
- **页面那一拍「要不要回答」只作记录**，真正开不开闸交给主链那道闸；这句话只要收住了就必须进主链——**判「不回答」或页面判断过期都不许静默吞句**（不对车的闲话多走一趟主链是已接受的代价）。开闸线仍是临时线，本页未改。
- **一句一办**：同一轮连说几句，各句各自进主链、各自有对人说的话上屏；后一句不把已收住的前一句粘进去。
- **办完有一句对人说的话上屏**（如「已打开主驾座椅按摩。」），不是「已执行：某工具」这类办理记录（办理记录只进台账）；回问照旧上屏。**本期不接语音播报**，回执与回问都以上屏为准。
- **说话席跟着输入进主链**：交给办理的输入里写明这句是主驾还是副驾说的；座椅按摩 / 加热这类要位置的工具**从输入里取位置**，**不回问位置**；话里点明位置的按话里位置办。
- **话筒可只开一路**：设备区「话筒」三档——两路都开（默认）/ 只开主驾 / 只开副驾；只开一路时只申请、采集、识别这一路，关着的那一席标「话筒关着」（仍可用文字说）；听音中切换会先停两路再按新选择重开。**真机开麦一键复验未做**（自动化浏览器没有麦克风权限，开麦这一层用页面内替身话筒跑同一段逻辑）。
- **垫句按模型选的那一档走**：真判断 `choice=cushion` 才垫、`choice=hold` 不垫；积极 / 连续说满 1.5 秒 / 要回答等前置条件保留；`P` 与 `confidence` 只留详情做诊断，不再双门槛否决。统一称「垫句」。**「模型选垫且条件齐、车真的垫一句」还没凑成，不得写成已完整验收通过。**
- **执行仍是 mock**：座椅按摩等办理是模拟，不接真车；不碰 8260、不触发真实导航或其他对外副作用。
- **单独问天气也算对车说**：判停 / 要不要回答这条链要给它开闸并回答；天气与车况是 PoC 的假数据（例：今天多云、气温 18 到 26 度），别当成接了真数据。不许为了凑这个效果去调低开闸线（线仍是临时线）。
- **接记忆**（2026-11-12 拍板）：这句收住之后，等**这次收住触发的那份读心落地**，再用判停同一套模型做一次很快的写前快判断（有没有必要写），通过才落一刀（碎片先攒再并）；判断为不写时页上写明原因（`memory_skip`）。
- 沿革：~~不接要不要回答、不开闸、不进 Agent（不建 `TurnSession`）~~ 是 2026-11-11 建页时的形态，已被上面九条取代（现在打开即接通正式答复、要回答那一拍只作记录、开闸交主链）。
- 细则见 `tmp/poc-neixinxi-page-DETAIL.md`、`tmp/poc-neixinxi-memory-write-DETAIL.md`、`tmp/poc-neixinxi-gate-seat-20261117-DETAIL.md`、`tmp/poc-neixinxi-three-fixes-20261117-DETAIL.md`、`tmp/poc-neixinxi-single-mic-20261117-DETAIL.md`、`tmp/poc-neixinxi-choice-formal-20261117-DETAIL.md`，离线契约 `tests/asr/test_neixinxi_page.py`、`tests/memory/test_neixinxi_memory.py`、`tests/memory/test_memory_write_judge.py`、`tests/gate/test_speaker_seat.py`，真链回放 `tests/asr/poc_neixinxi_wav_replay.py`。

读心页：http://127.0.0.1:8765/mind （同进程，`GET /mind`）。一屏四块：视频流 / 流式 ASR / **一句轻松旁白（稿 E，≤30 字）** / 中枢并发两路（说没说完 + 接不接）+ 闸门；接口 `GET /api/mind/demos`、`POST /api/mind/tick`、`POST /api/mind/reset`、`POST /api/hub/tick`（读心只出那句场景；接不接走 8009 真判断，开闸线未拍、本次临时用 0.5）。

读心引擎（页面三选一，刷新保持所选）：`/mind` 顶部选择器在 **`deepseek-v4.1-flash`（默认，关思考）** / **`cc/glm-5.2`** / **`cc/glm-5.3-flash`** 之间切换，每次 `POST /api/mind/tick` 带 `model`，服务端按 id 取引擎（进程内缓存，切模型不用重启）；选项直接标「能不能读图 + 大致快慢」，当前档在页面上可见。
- `deepseek-v4.1-flash`（默认档）：能读懂图；**只对这一档**加 `"thinking": {"type": "disabled"}`（另外两档不带），关思考中位约 2.3～3.4s。
- `cc/glm-5.2`：文本判断快准（同形 JSON 判断实测中位约 2～3.5s），但**当前端点读不懂图**（本单多图多次实测：数字恒错、纯红答白、翻转帧不跟图翻），页面与 `engine.vision.capability=unavailable` 都标「视觉不可用」，不再拿 `vision=ok` 冒充多模态成功。
- `cc/glm-5.3-flash`：能读懂图；**始终思考、后端 1210 明确不支持关闭**，故请求体不加 thinking 开关，中位约 5～12s（慢）。
`MIND_ENGINE=rule` 强制离线规则；`MIND_LLM_MODEL` 是无页面选择时的兜底（默认 `deepseek-v4.1-flash`，思考开关随该档口径）。真模型调用在后台线程跑，`tick` 不等模型；失败/超时回落规则并在 `engine{mode: rule_fallback, error}` 写明。超时仍 30 秒、`max_tokens` 仍 768（验收口径 512～768），`MIND_LLM_TIMEOUT` / `MIND_LLM_MAX_TOKENS` 可覆盖。画面按页面采好的**彩色 JPEG 原样透传**（默认 320×180，不转灰阶、不放大、不二次编码：送进模型的
`image_url` 就是那一段 data URI，`snapshot.frame` 回带同一段，页面照它显示）；先前「16×9 灰阶 → 160×90
最近邻放大」的口径作废（用户 2026-11-11 11:53 追加：衣服颜色、表情要靠颜色）；实测与口径见 `tmp/poc-mind-model-picker-DETAIL.md`（前序 `tmp/poc-mind-glm52-DETAIL.md`）。不连判停、不进 Agent、不 push。
