# 前端能力迁移与验收映射

本次采用中性专业工具方向；既有写接口和业务规则保留，无数据库迁移。新页面以真实接口为来源，没有内置模拟业务数据。

| 现有能力 | 新入口 | 接口 / 规则 | 验收场景 |
|---|---|---|---|
| 六平台下载 | 顶栏新建、任务页主要操作 | /tasks/download、/x/download、/platform-downloads/{platform}/download | 识别来源、单条与主页边界、提交后定位任务 |
| 跨平台队列 | 下载任务 | /operations/tasks | q / platform / status / sort / page 深链；刷新保持列表 |
| 任务详情 | 任务 Inspector | /operations/tasks/{platform}/{id} | 文件、进度、来源、错误与平台日志按需读取 |
| 暂停 / 恢复 / 取消 / 重试 | 行菜单、任务 Inspector | /operations/tasks/actions | 当前对象动作、部分失败、冷却与链接刷新 |
| 跨分页失败批处理 | 任务页更多菜单 | retry-all-failed、delete-all-failed 的 preview / job 接口 | 预演数量、范围不受搜索与当前页选择限制、投递结果与下载结果区分 |
| 抖音批量控制 | 抖音任务视图更多菜单 | /tasks/pause-all、redispatch-pending、refresh-retry-all-failed | 跨全部抖音任务的明确范围确认 |
| 记录删除 | 任务 Inspector / 批处理 | 原任务 DELETE、统一 actions | 保留磁盘文件；统一失败批处理继续保护部分下载记录 |
| 预览与互动趋势 | 任务 / 作品 Inspector | 原 preview / media / stats | 本地预览、不可用反馈、统计指标与快照 |
| 作者订阅 | 作者平台选项卡 | /authors、/x/authors 的 subscribe / unsubscribe | 开关反馈、平台能力差异、X 服务端检索 |
| 抖音作者作品 | 作者菜单、作者 Inspector | /authors/{id}/works | 分页、搜索、状态、类型、时间与排序；完整浏览 |
| 作者资料历史与同步 | 作者 Inspector | profile-history、sync-avatar | 名称 / 头像变化、低频资料同步 |
| 作者自动化配置与轨迹 | 作者 Inspector | auto-update、作者 PUT | 有效间隔、下一次预计检查、运行反向定位；X 沿用已有间隔，不虚构编辑能力 |
| 作者检查 / 对账 / 下载 | 作者 Inspector / 更多菜单 | check、reconcile、download、check-all | 当前作者与平台范围、风控保护 |
| 单作品与单图管理 | 作品 Inspector | /works/{id}、/works/{id}/files/{index}、redownload、retry-failed | 单图只删除对应文件，排除规则和重新下载规则保留 |
| 删除作品 | 作品菜单与选择栏 | 原 DELETE / batch-delete | 明确清理任务、文件并防止订阅自动重下 |
| 删除作者 | 作者配置危险区 | 原作者 DELETE | 抖音硬删除作品 / 任务 / 文件；X 删除用户及关联数据库记录，磁盘文件保留 |
| Run / Cycle | 自动化 | subscriptions?paginated=true、subscriptions/{id} | 时间始终可见、Cycle 与分页无关、详情按需读取 |
| 自动化诊断 | 自动化更多菜单 | subscriptions/diagnostic | 完成、超时、冷却、隔离、中断与未知状态 |
| 配置中心 | 设置目录 | config/all、runtime、archive-rules、平台 cookie、douyin-account | 脏字段、敏感值不回显、部分失败、未保存保护、动态生效 |
| 目录兼容 | 设置 / 下载目录 | config/all?preview=true | 预演关联路径数量、保存保留相对路径与预览行为 |
| 服务与平台就绪 | 系统目录 | process/status、status/readiness、operations/platform-readiness | 未确认状态不能显示为已停止或正常 |
| 存储巡检与恢复 | 系统 / 存储 | storage-audit、storage-repair、repair-all、journals、restore | 先预演，隔离可恢复，保留逐项结果 |
| 日志、版本与系统入口 | 系统目录 | logs、update 原接口、/docs、/legacy | 原始诊断披露、敏感边界、保留旧入口 |
| 高级后台诊断 | 系统 / 服务折叠区 | celery-debug、worker-log、celery-test、celery-purge-old、service/restart | 明确测试 / 旧队列清理 / 重启影响，保持原进程管理 |
| 数据库连接验证 | 设置 / 高级连接 | /config/database/test | 测试当前表单，不自动保存；空密码使用已有值 |

## 兼容与能力边界

旧 /douyin/tasks、/x/tasks 及其他平台 tasks 链接映射为统一任务的平台视图；旧作者、作品、自动更新与设置深链保持映射。未开放的小红书作者页面继续不挂接路由；小红书仍只支持单条笔记。旧 Vue 平台组件暂留源码，/legacy 保留到真实业务验收结束。

前端对象集中声明 Task、Author、Work、Run、Cycle；状态在 workspace.ts 管理。设计契约在 DESIGN.md。设置表单、系统内容与配置控制器分离，业务页面不再自行复制导航和表单保存流程。

## 验证边界

本地浏览器采用拦截接口的明确演示数据，验证组件、深链、焦点、移动布局、保护和反馈；不执行生产下载、删除、通知或维护。实际下载链路、长时间性能、屏幕阅读器及线上动态配置仍需在真实环境验收后再移除旧入口。临时验证文件位于忽略目录，验证结束删除，不提交。

## 本轮本地结果（2026-10-07）

- Vue 类型检查及 Vite 生产构建通过，构建输出写入实际首页使用的 static/app；业务页面按路由加载。
- 浏览器验证通过 5 个指定宽度、7 个主要页面的 35 组布局，未发现页面整体横向溢出或运行异常。
- 通过模拟下载识别 / 提交 / 定位、任务筛选与 Inspector、菜单确认取消后的焦点恢复、命令面板叠层、作者资料历史、X 检索、单文件删除取消、设置未保存保护、部分保存失败与旧数据反馈。
- 只读接口契约通过：摘要延迟加载、历史分页不改变 Cycle、旧报告调用兼容、搜索通配符转义、预演路由优先级、缺失对象 404、视频文件媒体类型。
- 新接口与变更文件通过 Python 3.12 语法检查。以上验证没有执行生产写入；真实媒体链路、线上配置与维护验收仍保留旧入口供核对。
