# docs · 文件对照

整理时把原先散在 `tmp/` 的文档改成了不带 `poc-streaming-` 前缀的名字，对照如下：

| 旧路径（tmp/） | 现在 |
|---|---|
| `poc-streaming-kev-eot-spec-20261015.md` | `docs/spec-20261015.md`（输入 spec，权威） |
| `poc-streaming-kev-eot-plan-20261015.md` | `docs/plan-20261015.md`（输入设计稿） |
| `poc-streaming-progress.md` | `docs/progress.md`（会话进度，持续追加） |
| `poc-streaming-final-report.md` | `docs/final-report.md` |
| `poc-streaming-prefix-eval-report.md` | `docs/prefix-eval-before.md` |
| `poc-streaming-prefix-eval-report-after.md` | `docs/prefix-eval-after.md` |
| `poc-streaming-m3-*.md` | `docs/m3-*.md`（7 份调参记录） |
| `poc-streaming-latency-opt-spec.md` | `docs/spec-latency-opt.md`（延迟优化 spec） |
| `poc-core-score-tools-spec.md` | `docs/spec-core-score-tools.md`（core_score 工具集替换 spec，权威） |

原 `tmp/` 文件未删除，仍可回溯；本目录是整理后的正式归档。

## 后加的口径页（不在上面的改名对照里）

| 文件 | 是什么 |
|---|---|
| `docs/no-confirm-window.md` | **去确认窗口径**（2026-11-04 拍板）：判停三态挂窗 / 生成中打断承接 / 落地即不可撤 / 参数面板与 `turn_reopen`（原 `barge_in`）命名口径。图、诊断面板、术语的文案基准；实现归「去窗单」 |
| `docs/test-design-dual-asr-text-cases.md` | 双路 ASR 文本用例与断言面契约（2026-11-03） |
| `docs/test-design-confirmation-v2.md` | 二次确认 v2 验收设计（2026-11-03） |
