流式完成：POST /api/run/stream + fetch ReadableStream；已重启并保持 http://127.0.0.1:8765/ 运行（PID 15104）。旧页面需刷新一次。

「导航回家」验证 PASS：accepted → kev_start → kev_scores → llm_start → llm_decision → exec_start → exec_step(调用模拟) → exec_step(mock成功) → exec_done → done。客户端实际到达约 12 / 29 / 447 / 447 / 1826 / 1827 / 1828 / 1828 / 1831 / 1832 ms。KEV 14分、top-k3：navi_to_special_poi(0.9821)、navi_start_route(0.8029)、navi_open_address_page(0.5156)；真LiteLLM deepseek-v4.1-flash 决策 tool_call navi_to_special_poi({poi_type:home})；executed_tools=0，mock_only=true。

验证使用实际页面 JS（DOM shim）消费真实 HTTP SSE：llm_start 到达时已有14行分数且无决策；llm_decision 后才显示 mock 步骤，不是完整 run() 结束后回放。完整事件/到达时间：[sample-stream-navigation-home.json](</Users/YUYSONG/Work/tmp/jev-research/poc-ui/sample-stream-navigation-home.json>)。ask_user/done 保持阶段顺序，以 skipped=true 说明跳过执行，不产生 exec_step。

最小改动：core_pipeline_run.run 新增可选 on_stage 回调，默认不变；server.py 用同步单句路径的实时回调推送 SSE，再展示两条纯 mock 步骤；index.html 流解析和时间线就地更新；README 更新。未做重连、历史、并发编排、鉴权或真实工具执行；未提交或 push。

NOTE：未做 llm_delta。现有 slow_turn 是非流式响应解析，token 流需要另加增量协议解码；本轮按阶段流式达标，不扩展模型客户端，也未测试或声称 LiteLLM 不支持 stream:true。沿用现有凭证与进程内 NO_PROXY 修正。到此停止。
