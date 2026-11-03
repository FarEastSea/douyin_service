# 开源许可与第三方组件

## 项目许可

本项目继续使用仓库现有的 [GNU AGPL v3](../LICENSE)。它适合希望保留服务端修改版本源码可获取性的 Web 项目。许可证允许使用、修改、再分发和商业使用；修改版本通过网络提供交互服务时，第 13 条要求向相关用户提供该版本的对应源码。完整权利与义务以许可证正文为准。

开源许可授予的是软件使用权，不授予平台内容的版权、账号权限或采集授权。README 中的使用边界也不替代许可证，不对开源权利增加“禁止商用”等额外限制。

## 第三方许可边界

第三方库、独立工具和引用实现保留各自的许可证，不能因本仓库标注 AGPL 而被重新许可。已引用的实现及来源见[第三方声明](../THIRD_PARTY_NOTICES.md)。

当前 `gallery-dl==1.32.17` 声明为 **GPL-2.0-only**。Instagram 运行器直接导入其 Python 模块；即使运行器处于子进程中，也不能仅凭进程隔离断言许可兼容。这一集成存在需要进一步审查的 GPLv2 与 AGPLv3 兼容性问题。不能通过更换本仓库的 LICENSE 文件重新许可上游代码；也不宜改为 GPLv2，因为仓库还包含 Apache-2.0 来源实现，后者与 GPLv2 不兼容。

因此，本次保留现有 AGPL 许可，并明确记录上述集成边界，不宣称整个依赖组合已完成许可合规审查。解决该项需要核实可用的上游授权，或调整实现及集成边界；分发整合包和提供服务前应结合实际版本及交互方式审查。

## 参考资料

- [GNU AGPL 使用说明](https://www.gnu.org/licenses/why-affero-gpl.html)
- [GNU 关于 GPLv2 与 GPLv3 的兼容性说明](https://www.gnu.org/licenses/gpl-faq.html#v2v3Compatibility)
- [Apache 关于 GPL 兼容性的说明](https://www.apache.org/licenses/GPL-compatibility)
- [gallery-dl 固定版本源码](https://github.com/mikf/gallery-dl/tree/v1.32.17)
