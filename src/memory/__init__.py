"""记忆模块：memos（MemOS Cloud）真写真查。

口径（与 wiki `四模块职责与接口契约` §1 记忆行、§2 接口 3、§4 开闸分流一致）：

- 写：**中枢把这句收住那一刻**（判停给「说完」/ 静音窗收口），由 `gate/turn_session._confirm`
  调 `write(turn)`，`turn` 带这句完整原话 + 这一刻的场景；**只进上下文的话也写**。
- 查：**Agent 发起请求那一刻**（`_start_llm` 提交决策之前）调 `recall(query=本句原话)`。
- 记什么由 memos 自己抽：我们只决定送进去的原料（原话 + 场景 + 席位标签），本地不加「只留人名/席位」
  这类过滤；能记成什么样见交付 DETAIL 的实测记录。

文件：

    interface.py  形状与时机（MemoryStore 协议）
    memos.py      MemosMemoryStore（真实现）/ NullMemoryStore（没开）/ build_store（按环境装配）

凭据只从进程环境 `MEMOS_API_KEY` 或 `MEMOS_ENV_FILE` 指向的已有文件**只读**就地取，不复制、不打印。
"""
