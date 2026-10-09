# 提醒规则来源与实施边界

产品语义见[需求P-07](requirements.md)，评估协议见[ADR0035](../architecture/decisions/0035-reminder-rule-evaluation.md)。本目录只记录证据与公式边界；实现进度见[当前状态](../engineering/status.md)。核验日期：2026-10-10（Asia/Shanghai）。

规则版本固定为`2026-10-10.1`；这表示本次核验版本，不表示法规在该日生效或永久有效。仅使用官方来源，不把“网页仍可访问”当作其内容仍适用于全部事项的证明。适用性由用户/会计确认；不自动收集或提交真实公司资料。

## 候选目录

| 稳定规则ID | 地区与对象 | 公式依据与必须提供的资料 | 实施边界 |
| --- | --- | --- | --- |
| `cn.company.annual_report` | 中国大陆有限责任公司 | 注册日期、申报年度；向登记机关报送上一年度报告的窗口为1月1日至6月30日，设立当年从次年开始。来源CN1第八条。 | 计算目标申报年度6月30日；不套用于税务申报，也不推断个别延期或其他公示义务。 |
| `us.wy.llc.annual_report` | 美国WY LLC | 初始登记日期、目标申报年度；次年起每年初始登记月份的1日。来源WY1，例示2月16日登记、次年2月1日到期。 | 不以“周年日”误算成原月日；不套用于NM或联邦税务，不凭其他州规则顺延周末。 |
| `hk.private.annual_return` | 香港本地私人有限公司 | 注册周年或迁册周年及目标年度；非成立/迁册当年，周年后42天。最终一天为星期日/公众假日时顺延，普通星期六不顺延。来源HK1/HK2。 | 须区分本地注册/迁册、休眠豁免和适用周年。闰日周年解释、未知年份日历及特殊天气须进一步核验；有未覆盖条件时只显示基础日期并标待确认。 |
| `ee.private.annual_report` | 爱沙尼亚OÜ | 实际报告期末；年度报告在财年结束后6个月内提交，来源EE1。 | 不假定所有公司12月31日结账；不得从成立日期猜测首个或变更后的实际报告期间。月底对应日和非工作日调整另核验后才能把基础日期标为可采用。 |
| `certificate.expiry` | 已记录有效期的证件 | 用户核对的证件到期日；原样采用，不从国家或成立日期猜有效期。 | 提醒的是证件到期，不能冒充另一个申报/缴费截止日。香港商业登记证有效期及续期见HK4/HK5。 |
| `us.nm.llc.annual_report` | 美国NM LLC | NM1区分LLC法与公司报告法；NM2列明LLC备案事项。 | 本次尚未取得可充分支持自动年度期限或豁免结论的官方条文；返回待核验，不复制WY公式，也不宣称“没有任何申报义务”。 |

一般税务申报不由上述目录推定；税务身份、财政年度、登记状态、主管机关通知和适用延期需要分别核验。未核验事项使用人工日期，并保留理由与依据。证件扫描/OCR不能自动确认法规适用性。

## 官方来源

| 编号 | 官方文件/页面 | 本目录使用的事实 |
| --- | --- | --- |
| CN1 | [国家市场监督管理总局：企业信息公示暂行条例（2024年修订）](https://www.samr.gov.cn/xyjgs/flfg/art/2024/art_be55c2e3a54a43e5ab12794c9dc87600.html) | 第八条的报告窗口、上一年度和次年开始。 |
| WY1 | [Wyoming Secretary of State：What's Next](https://sos.wyo.gov/Forms/WyoBiz/What%27s_Next.pdf) | 第1页第1项，登记月1日及首次申报年度。 |
| HK1 | [香港公司注册处：周年申报表常见问题](https://www.cr.gov.hk/en/faq/local-company/annual-return.htm) | Q3/Q4的星期日、公众假日和星期六；Q15的周年与首年；私人公司休眠条件。 |
| HK2 | [香港公司注册处：周年申报表交付日期计算器](https://www.cr.gov.hk/en/compliance/annual-return/calculator.htm?year=2025) | 最终日遇特别天气等条件的说明；不是可预测未来天气的资料源。 |
| HK3a | [香港政府：2026年公众假期](https://www.info.gov.hk/gia/general/202505/16/P2025051300353.htm) | 2025-05-16公布的公众假日日历；不用劳工法的法定假日列表替代。 |
| HK3b | [香港政府：2027年公众假期](https://www.info.gov.hk/gia/general/202605/15/P2026051400300p.htm) | 2026-05-15公布的公众假日日历；未公布/未核验年份不能假定无假日。 |
| HK4 | [香港税务局：商业登记证有效期](https://www.ird.gov.hk/eng/tax/bre_vbr.htm)、[一或三年证书](https://www.ird.gov.hk/eng/tax/bre_lcc.htm) | 以证书所载到期日为准，不能统一按成立周年算一年。 |
| HK5 | [香港税务局：商业登记续期](https://www.ird.gov.hk/eng/tax/bre_rbr.htm) | 到期前发送续期通知；未收到通知的告知期限不能误用为缴费截止日。 |
| EE1 | [Estonian Centre of Registers and Information Systems：Annual report](https://www.rik.ee/en/e-business-register/annual-report) | References to legislation：报告期末后6个月，即使无经营也要报送。 |
| NM1 | [New Mexico Secretary of State：Statutes governing business](https://www.sos.nm.gov/business-services/statutes-governing-business-in-nm/) | LLC Act与Corporate Reports分别列出；目录本身不足以证明年度报告豁免。 |
| NM2 | [New Mexico行政规则12.3.4](https://www.srca.nm.gov/parts/title12/12.003.0004.html) | 12.3.4.11/12的LLC备案范围；不把未列某事项直接等同法定豁免。 |

## 核验与更新

- 修改规则必须保留旧版本及来源，追加新版本；节假日日历具有明确年份/来源，不能延用到其他年份。
- 未来特殊天气、个别主管机关延期和用户资格变更不可能仅靠静态规则获知。界面保留推算依据/核验日期及待确认原因，人工覆盖记录独立存在。
- 新增自动规则前，用官方文字核对适用对象、首个报告期、边界日期与例外，再加入明确虚构案例；纯函数测试通过仅证明实现符合该公式。
