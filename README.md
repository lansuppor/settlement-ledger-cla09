# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；订单批量导入（把一批订单以 CSV 提交为可校验、可断点续跑的导入任务，逐行独立判定并受理）；退款单的受理、读取、冲正与幂等重放；结算单的受理、读取、推进（把引用的待处理退款原子推进为已生效）与撤销（解除已生效结算单的全部核销，退款回到待处理）；订单对账（按订单核对已收、退款与已生效结算合计并留痕，不改变任何单据状态）；异常工单的登记、读取、处理、解决与关闭（把异常处理诉求登记成可跟踪的独立单据，解决受对账未结清冲突约束）；以及出入库单的受理、按标识读取、条件检索（按订单、方向、数量区间、创建时间区间任意组合过滤，按创建时间+标识稳定排序的游标分页）与撤销（把误登记的出入库单置为终态 `reversed`，不再计入数量汇总）。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /order-imports`：提交批量导入任务并立即逐行校验受理。请求字段 `task_id`、`csv_content`（完整 CSV 文本，可含 `\n` 转义；表头必须恰为 `tenant,order_id,amount_cents,currency`，与 `fixtures/orders.csv` 一致；行内字段会去除首尾空白；完全空白行不计入数据区）；任务归属租户经 `X-Tenant` 传入。受理成功返回 201 与任务对象（正常情况下任务已执行完成，状态为 `completed`）；缺租户头或 CSV 形态不合法（空内容、表头不符、只有表头无数据）返回 400；同一任务标识不同内容再次提交返回 409 且不改变已存在任务与订单数据；同一任务标识相同内容再次提交视为重放，继续把未决行跑完后返回与首次相同的业务结果（含首次错误结果），不产生第二次受理。无需 `Idempotency-Key`——同标识同内容本身即重放凭证（内容以 SHA-256 指纹持久化，重启后仍识别）。
- `GET /order-imports/{task_id}`：按标识读取导入任务，返回 `task_id`、`tenant`、`status`（`pending`/`completed`）、`total_rows`、`success_count`、`failure_count`、`processed_count`、`errors`（逐条 `line_number`、`raw_line`、`reason`，行号按数据区从 1 起）、`created_at`、`completed_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404。成功受理的订单可直接经 `/orders` 等既有链路使用，任务读取不要求行内租户与提交租户一致。
- `POST /order-imports/{task_id}/resume`：断点续跑。只处理任务中尚未处理的行，已成功行不重复受理、已判失败行不重复记录；返回 200 与最新任务对象。任务不存在或跨租户返回 404。执行中失败或中断的任务保持 `pending`，可反复续跑直至完成；已完成任务续跑为幂等成功。
- `POST /refunds`：受理退款单。请求字段 `refund_id`、`order_id`、`amount_cents`、`reason`；租户经 `X-Tenant` 传入，幂等键经 `Idempotency-Key` 请求头传入（必填）。受理成功返回 201，退款单为 `pending`。参数不合法（金额非正整数/超过订单金额）返回 400/422；订单不存在或跨租户返回 404（不泄漏对象是否存在）；退款标识重复受理返回 409；超过可退金额返回 409。
- `GET /refunds/{refund_id}`：按标识读取退款单，返回 `refund_id`、`order_id`、`amount_cents`、`reason`、`status`、`created_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404。
- `POST /refunds/{refund_id}/reverse`：冲正退款单。对待处理（`pending`）或已生效（`effective`）退款单冲正后进入终态 `reversed` 并立即释放可退额度，返回 200；对已冲正退款单重复冲正返回 409 且不改变状态；不存在或跨租户返回 404。需带 `Idempotency-Key`。
- `POST /settlements`：受理结算单。请求字段 `settlement_id`、`order_id`、`amount_cents`（最小货币单位正整数）、`refund_ids`（非空、去重的退款标识集合）、`reason`；租户经 `X-Tenant` 传入，`Idempotency-Key` 必填。成功返回 201，结算单为 `pending`；参数不合法返回 400/422；订单不存在或跨租户返回 404（不泄漏对象是否存在）；结算标识重复受理返回 409。
- `GET /settlements/{settlement_id}`：按标识读取结算单，返回 `settlement_id`、`order_id`、`amount_cents`、`refund_ids`、`reason`、`status`、`created_at`、`updated_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404。
- `POST /settlements/{settlement_id}/advance`：结算推进。原子地把引用的待处理退款全部置为 `effective` 且结算单进入 `effective`，返回 200；任一引用不满足则全部保持原状并返回可区分错误：引用退款不存在（含属于其他订单者）404，已被其他结算单核销 409，已冲正 409，结算金额与引用退款合计不符 409；结算单不存在或跨租户 404；已生效结算单重复推进 409；对已撤销结算单推进 409。需带 `Idempotency-Key`，同一（租户、操作、结算单标识、请求指纹）重放返回首次结果（含首次错误）；不同幂等键并发推进同一结算单仅一个成功，其余 409。
- `POST /settlements/{settlement_id}/revoke`：结算撤销。仅已生效（`effective`）结算单可撤销；原子地解除该结算单持有的全部核销声明、将其核销过且当前仍为 `effective` 的退款单置回 `pending`（继续占用可退额度，可被其他待处理结算单重新引用并在推进时核销），结算单进入终态 `revoked`，返回 200 与最新结算单对象。任一不满足则全部保持原状并返回可区分错误：结算单不存在或跨租户 404（不泄漏对象是否存在）；已撤销结算单重复撤销 409；待处理（`pending`）结算单撤销 409。撤销只解除核销关系，不改变退款单本身状态——推进后又被冲正的退款在撤销时保持 `reversed`。退款冲正仍只作用于退款单，与撤销互不替代。需带 `Idempotency-Key`，同一（租户、操作、结算单标识、请求指纹）重放返回首次结果（含首次错误，如首次为 404/409 则重放仍为同一 404/409），不产生第二次撤销或第二次状态翻转；不同幂等键并发撤销同一结算单仅一个生效，其余 409；撤销与推进并发作用于同一结算单或同一批退款时也仅一个操作生效，另一个被拒绝且不改变已存在数据。
- `POST /reconciliations`：发起并执行订单对账批次。请求字段 `batch_id`、`order_id`、`note`（核对说明，可空）；租户经 `X-Tenant` 传入，`Idempotency-Key` 必填。在同一事务内读取订单当前已收金额、待处理退款合计、已生效退款合计与已生效结算单合计，得出已核销结余（= 已收金额 −（待处理 + 已生效）退款合计）与核对结论（`balanced`/`mismatched`），批次进入 `completed` 并返回 201 与完整批次对象；对账只核对与留痕，不改变订单、退款、结算单的任何状态与金额。订单不存在或跨租户返回 404（不泄漏对象是否存在）；批次标识重复返回 409；同一订单已有进行中批次或执行期间有并发写入（收款、退款受理/冲正、结算推进/撤销、另一批次对账）返回可区分的 409，且整体失败、不留进行中批次或部分结论。同一订单同时只允许一个进行中的对账批次。
- `GET /reconciliations/{batch_id}`：按批次标识读取对账批次，返回 `batch_id`、`order_id`、`note`、`paid_cents`、`pending_refund_cents`、`effective_refund_cents`、`effective_settlement_cents`、`settled_balance_cents`、`conclusion`、`status`、`created_at`、`completed_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404（不泄漏对象是否存在）。已完成批次的结论不随订单后续收支变化而追溯修改，需要时以新批次标识重新发起即可。
- `POST /tickets`：登记异常工单。请求字段 `ticket_id`、`order_id`、`issue`（问题说明，非空）；租户经 `X-Tenant` 传入，`Idempotency-Key` 必填。成功返回 201，工单为 `pending`（待处理）。参数不合法返回 400/422；目标订单不存在或跨租户返回 404（一律按不存在处理，不泄漏对象是否存在，且不留工单）；同一租户工单标识重复受理返回 409 且不改变已存在工单。
- `GET /tickets/{ticket_id}`：按标识读取工单，返回 `ticket_id`、`order_id`、`issue`、`status`、`created_at`、`updated_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404。
- `POST /tickets/{ticket_id}/process`：处理工单。把待处理（`pending`）工单推进为处理中（`processing`）；对已处于 `processing` 的工单处理为幂等成功（不再次翻转、不改变更新时间）；已解决（`resolved`）或已关闭（`closed`）工单处理返回 409 且不改变状态；不存在或跨租户返回 404。需带 `Idempotency-Key`。
- `POST /tickets/{ticket_id}/resolve`：解决工单。把 `pending` 或 `processing` 工单置为 `resolved`，并记录请求字段 `resolution_note`（解决说明，非空）。已关闭工单解决返回 409；对已解决工单再次解决返回 409。**对账未结清冲突**：若该工单所属订单存在结论为不符（`mismatched`）且已完成（`completed`）的对账批次（以最新一条已完成批次的结论为准），解决被拒绝返回 409 且不改变工单状态；需先以新批次重新对账得到相符（`balanced`）结论后才可解决。已关闭工单不受此限制。不存在或跨租户返回 404。需带 `Idempotency-Key`。
- `POST /tickets/{ticket_id}/close`：关闭工单。把 `pending`、`processing` 或 `resolved` 工单置为终态 `closed`，返回 200；对已关闭工单重复关闭返回 409 且不改变状态；不存在或跨租户返回 404。需带 `Idempotency-Key`。
- `POST /stock-movements`：受理出入库单。请求字段 `movement_id`、`order_id`、`direction`（仅 `in` 入库 / `out` 出库）、`quantity`（正整数）；租户经 `X-Tenant` 传入，幂等键经 `Idempotency-Key` 请求头传入（必填）。受理成功返回 201，原样保留方向与数量，并返回 `movement_id`、`order_id`、`direction`、`quantity`、`status`（`accepted`）、`created_at`。参数不合法返回 400/422；目标订单不存在或跨租户一律按不存在处理，返回 404（不泄漏对象是否存在）；同一租户出入库单标识重复受理返回 409 且不改变已存在单据。出入库单只作用于自身，不改变订单、退款、结算单、对账批次、工单与批量导入任务的状态、金额与结论。
- `GET /stock-movements/{movement_id}`：按标识读取出入库单，返回 `movement_id`、`order_id`、`direction`、`quantity`、`status`（`accepted`/`reversed`）、`created_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404（不泄漏对象是否存在）。
- `POST /stock-movements/{movement_id}/reverse`：撤销（冲正）出入库单。对本租户已受理（`accepted`）的出入库单撤销后置为终态 `reversed`，返回 200 与最新单据对象（字段同按标识读取，含 `status`）；`reversed` 单据不计入数量汇总。单据不存在或跨租户按不存在处理返回 404（不泄漏对象是否存在）且不改变数据；对已撤销单据重复撤销返回 409 且不改变状态。需带 `Idempotency-Key`，按（租户、操作、出入库单标识、`Idempotency-Key`）去重：同一指纹重放返回与首次完全相同的结果（含首次错误，404/409 重放仍相同），不产生第二次撤销或状态翻转，去重记录持久化落库、重启后仍识别；不同指纹并发撤销同一标识仅一个生效，其余 409 且不改变数据。
- `GET /stock-movements`：条件检索本租户出入库单。查询参数可任意组合（条件之间为逻辑与、区间含端点）：`order_id`、`direction`（`in`/`out`）、`quantity_min`、`quantity_max`（正整数，`quantity_min` 不得大于 `quantity_max`）、`created_from`、`created_to`（ISO 时间，含端点）、`status`（`accepted`/`reversed` 精确过滤）、`include_reversed`（布尔；与 `status` 不得同时给出，否则 400）、`cursor`（上一页返回的续页游标）、`limit`（每页上限，默认 100、最大 500，须为正整数）。`status` 与 `include_reversed` 都不传时默认只返回 `accepted` 单据；`include_reversed=true` 返回全部状态。返回 `{"items":[...],"next_cursor":...}`，每条字段与按标识读取一致（含 `status`）；按创建时间升序、相同则按标识升序排序，顺序不受插入先后与重复查询影响。结果数超过当前页上限时返回不透明 `next_cursor` 续取，否则为 `null`；游标指向上页末条在该排序中的位置，任意页序并集恰等于完整结果集、不重复不漏项（含冲正单据的结果集同样成立）。检索按租户隔离，空结果返回空列表 `items` 且 `next_cursor` 为 `null`；租户头缺失或参数不合法返回 400/422，游标非法返回 400。

### 工单与对账未结清说明

- 工单状态机：待处理（`pending`）→ 处理中（`processing`）→ 已解决（`resolved`）→ 已关闭（`closed`，终态）；`pending`/`processing` 也可直接解决或关闭。受理成功即为 `pending`。
- 所有状态翻转均为单事务原子操作，失败不改变任何已存在数据；工单的登记与翻转只作用于工单本身，不改变订单、退款、结算单与对账批次的任何状态、金额与结论。
- 解决闸门以「该订单最新一条已完成对账批次的结论」为准：最新为 `mismatched` 即拒绝解决（409，可区分错误）；最新为 `balanced`（或尚无已完成批次）才允许解决。历史不符批次不追溯、不阻塞——以新批次重新对账得到相符即可。
- 工单四类写操作（登记、处理、解决、关闭）均以（租户, 操作, 工单标识, `Idempotency-Key`）幂等去重：同一指纹重复提交返回与首次完全相同的业务结果（含首次错误，如首次为 404/409 则重放仍为同一 404/409），不产生第二次登记或第二次状态翻转；去重记录持久化落库，重启后仍识别为重放。不同指纹指向同一工单并发提交时仅一个生效，其余 409 且不改变已存在数据。
- 错误可区分：参数不合法 400/422；对象不存在 404；重复受理 409；非法状态翻转 409；对账未结清冲突 409（detail 以 `order has a mismatched reconciliation batch` 开头）。

### 退款与幂等说明

- 可退金额 = 订单已收金额 − 待处理/已生效退款合计；`reversed` 退款不占用额度。
- 冲正为单事务原子操作，失败不会留下部分占用或部分释放。
- 结算推进为单事务原子操作：引用退款全部进入 `effective` 且结算单进入 `effective`，或全部保持原状；同一退款单只能被一张已生效结算单核销（声明表唯一约束兜底）。
- 结算撤销为单事务原子操作：结算单进入终态 `revoked`、其全部核销声明解除、退款回到 `pending`，要么一并发生要么全部保持原状。撤销后退款可被另一张结算单重新核销，但同一退款同一时刻仍至多被一张已生效结算单核销；撤销只解除核销关系，不改变退款单状态（与退款冲正互不替代）。
- 幂等重放以（租户, 操作, 目标标识, `Idempotency-Key`）去重：同一键重复提交返回与首次完全相同的业务结果（含首次错误），不会产生第二次受理、第二次扣减、第二次释放、第二次状态翻转或第二次撤销；重启后依然识别为重放。不同幂等键指向同一目标标识并发提交时仅一个生效，其余返回 409。工单的登记、处理、解决、关闭同样按此口径去重。

### 对账说明

- 对账批次为只读核对：发起即在同一事务内完成核对并留痕，批次状态由 `in_progress` 进入 `completed`（终态）；不改变订单、退款、结算单的任何状态与金额。
- 核对口径：已收金额 = 待处理退款合计 + 已生效退款合计 + 已核销结余，其中已核销结余 = 已收金额 −（待处理 + 已生效）退款合计；占用额度 ≤ 已收金额 ≤ 订单金额时结论为 `balanced`，否则为 `mismatched`。
- 同一订单同时只允许一个进行中的对账批次（部分唯一索引兜底）；并发发起时仅一个进入进行中，其余返回 409。
- 执行期间订单被收款、退款受理/冲正、结算推进/撤销等并发写入改动时，对账要么基于改动前的读数原子完成并留痕，要么整体失败并返回可区分的 409 冲突原因，不留进行中批次或部分结论。
- 对账幂等以（租户, 操作, 批次标识, `Idempotency-Key`）去重：重复提交返回与首次完全相同的业务结果（含首次错误），不产生第二次对账、第二次留痕；重启后仍识别为重放。
- 已完成批次的结论不随订单后续收支变化而追溯修改；需要重新核对时以新批次标识再次发起。

### 批量导入说明

- 导入任务以（提交租户, 任务标识）唯一，任务状态只有未完成（`pending`）与已完成（`completed`，终态）。
- 逐行独立判定，单行失败不影响其他行。合格行受理为订单，状态与字段与 `POST /orders` 逐单受理完全一致（`accepted`、`paid_cents=0`），后续读取、收款、退款、结算、对账、工单各条链路行为一致；不合格行不受理，只在错误清单记录原因。失败原因包括：金额须为正整数（`amount_cents must be a positive integer`）、币种须受支持（`unsupported currency: ...`，支持 CNY/USD/EUR/JPY）、订单标识在该租户下已存在（`order already exists for tenant`）、行须恰为 4 列、租户/订单标识非空。
- 批内去重：完全相同的重复行只受理先到的一行，其余记 `duplicate row in import: identical line already present`；不同内容但（租户, 订单标识）相同的行也只受理先到的一行，其余记 `duplicate order in import: order_id already used by an earlier line`。批内重复判定优先于其他校验。
- 每行在独立的立即写事务内判定：订单插入（或失败记录）与任务计数同事务提交，故整批执行失败（如服务中断）不会留下半行结果，也不会把同一行受理两次；中断时任务保持 `pending`，已提交的行即为已决行。
- 断点续跑：`POST /order-imports/{task_id}/resume`（或以同标识同内容再次 `POST /order-imports`）只处理尚未处理的行，不重复受理已成功行、不重复记录已判失败行；任意导入与续跑序列后，每行要么被受理一次、要么被判失败一次，始终满足 成功数 + 失败数 = 已处理数，全部完成时 已处理数 = 总行数，最终计数与错误清单与一次性成功导入一致。
- 重放与冲突：同一任务标识相同内容重复提交返回与首次相同的业务结果（含首次错误结果），不产生第二次导入或第二次受理；内容指纹（SHA-256）持久化在任务行上，服务重启后同一请求仍识别为重放。同一任务标识不同内容并发或先后提交时只允许一个生效，其余返回 409 且不改变已存在任务与订单数据（写事务串行 + 任务主键唯一兜底）。
- 行内租户逐行生效：导入可混合多个租户的订单；行内租户仅用于该行订单的归属与判重，不要求与提交任务的 `X-Tenant` 一致。跨租户读取导入的订单仍按不存在处理（404）。
- 错误可区分：参数不合法 400（CSV 形态错误等）；任务不存在或跨租户 404；任务标识以不同内容重复受理 409；服务内部错误 500（任务与未决行保持可续跑）。

### 出入库单说明

- 出入库单以（租户, 出入库单标识）唯一；受理需提供出入库单标识、订单标识、方向（`in` 入库 / `out` 出库）与数量（正整数）。受理成功后方向与数量保持原值，不再变更；状态生命周期为 `accepted`（已受理）→ `reversed`（已撤销，终态）。
- 受理时目标订单必须存在且属于同一租户；不存在或跨租户一律按「不存在」处理（404），不泄漏对象是否存在，且不留单据。同一租户标识重复受理返回 409 且不改变已存在单据（不以后到请求覆盖方向/数量）。
- 按标识读取返回出入库单标识、订单标识、方向、数量、状态与创建时间；跨租户读取按不存在处理（404）。
- 撤销（冲正）：`POST /stock-movements/{movement_id}/reverse` 把已受理单据置为终态 `reversed` 并返回最新单据；`reversed` 单据不计入数量汇总（每租户每订单净出入数量 = 已受理入库单数量和 − 已受理出库单数量和，冲正单据计 0）。单据不存在或跨租户按不存在处理（404）且不改变数据；重复撤销返回 409 且不改变状态。同一单据至多冲正一次，任意受理、撤销、重放、并发与重启序列后守恒关系成立。
- 幂等去重与其他写操作同口径：受理以（租户, 操作 `stock_movement_accept`, 出入库单标识, `Idempotency-Key`）去重，撤销以（租户, 操作 `stock_movement_reverse`, 出入库单标识, `Idempotency-Key`）去重；同一指纹重复提交返回与首次完全相同的结果（含首次错误，如首次为 404/409 则重放仍为同一 404/409），不产生第二次受理或第二次状态翻转；去重记录持久化落库，重启后仍识别为重放。不同指纹并发提交同一标识时仅一个生效，其余 409（写事务 `BEGIN IMMEDIATE` 串行 + 主键唯一兜底）。
- 检索为只读：支持按订单标识、方向、数量区间（`quantity_min`/`quantity_max`，含端点）、创建时间区间（`created_from`/`created_to`，含端点）、状态（`status` 精确过滤）任意组合过滤，条件之间为逻辑与；`include_reversed=true` 返回全部状态，`status` 与 `include_reversed` 同时给出返回 400，都不传默认只返回 `accepted`。按创建时间升序、相同创建时间按出入库单标识升序排序，顺序稳定，不受插入先后与重复查询影响。
- 游标式稳定分页：`cursor` 指向上一页末条在（创建时间, 标识）排序中的位置（不透明 base64），页内严格大于该位置续取，故不重复、不漏项；任意分页大小、任意取页顺序下，各页并集恰等于完整过滤结果集（含冲正单据的结果集同样成立）。结果数超过当页 `limit` 才返回 `next_cursor`，末页（含空结果）返回 `null`。检索按租户隔离。
- 出入库单的受理、读取、检索与撤销只作用于自身，不改变订单、退款、结算单、对账批次、工单与批量导入任务的状态、金额与结论；订单可退金额 = 已收 −（待处理 + 已生效）退款合计的守恒关系继续成立。
- 错误可区分：参数不合法 400/422（缺租户头/缺幂等键、方向非 in/out、数量非正整数、数量区间反向、`status` 非法、`include_reversed` 非布尔、`status` 与 `include_reversed` 同时给出、游标非法）；对象不存在（含跨租户）404；重复受理或重复撤销 409；服务内部错误 500。

### 调用示例

```bash
# 受理退款（同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/refunds -H 'X-Tenant: t1' -H 'Idempotency-Key: rfd-0001' \
  -H 'Content-Type: application/json' \
  -d '{"refund_id":"r1","order_id":"o1","amount_cents":300,"reason":"客户申请"}'

# 读取退款单
curl -s localhost:8000/refunds/r1 -H 'X-Tenant: t1'

# 冲正退款单（释放可退额度）
curl -s -X POST localhost:8000/refunds/r1/reverse \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: rev-0001'

# 受理结算单（引用退款集合，金额为最小货币单位整数且须等于推进时的退款合计）
curl -s -X POST localhost:8000/settlements -H 'X-Tenant: t1' -H 'Idempotency-Key: stl-0001' \
  -H 'Content-Type: application/json' \
  -d '{"settlement_id":"s1","order_id":"o1","amount_cents":300,"refund_ids":["r1"],"reason":"周期结算"}'

# 读取结算单
curl -s localhost:8000/settlements/s1 -H 'X-Tenant: t1'

# 结算推进：引用的待处理退款逐张生效，结算单进入 effective
curl -s -X POST localhost:8000/settlements/s1/advance \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: adv-0001'

# 结算撤销：解除全部核销，退款回到 pending，结算单进入 revoked（终态）
curl -s -X POST localhost:8000/settlements/s1/revoke \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: rev-stl-0001'

# 发起并执行订单对账（同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/reconciliations -H 'X-Tenant: t1' -H 'Idempotency-Key: rec-0001' \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"b1","order_id":"o1","note":"月度对账"}'

# 按批次标识读取对账结果
curl -s localhost:8000/reconciliations/b1 -H 'X-Tenant: t1'

# 登记异常工单（同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/tickets -H 'X-Tenant: t1' -H 'Idempotency-Key: tkt-0001' \
  -H 'Content-Type: application/json' \
  -d '{"ticket_id":"w1","order_id":"o1","issue":"收到商品与描述不符"}'

# 读取工单
curl -s localhost:8000/tickets/w1 -H 'X-Tenant: t1'

# 处理：待处理 -> 处理中（对处理中工单重复处理为幂等成功）
curl -s -X POST localhost:8000/tickets/w1/process \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: tkt-proc-0001'

# 解决：待处理/处理中 -> 已解决，并记录解决说明
# （若订单最新已完成对账批次结论为 mismatched，会返回 409 对账未结清冲突）
curl -s -X POST localhost:8000/tickets/w1/resolve \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: tkt-res-0001' \
  -H 'Content-Type: application/json' -d '{"resolution_note":"已补发并致歉"}'

# 关闭：任意非终态 -> 已关闭（终态）
curl -s -X POST localhost:8000/tickets/w1/close \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: tkt-close-0001'

# 批量导入订单（表头与 fixtures/orders.csv 一致；同标识同内容重放安全）
curl -s -X POST localhost:8000/order-imports -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"imp-0001","csv_content":"tenant,order_id,amount_cents,currency\nt1,bulk-1,1200,CNY\nt1,bulk-2,800,CNY\nt2,bulk-3,4500,USD\n"}'

# 读取导入任务（含计数与逐行错误清单）
curl -s localhost:8000/order-imports/imp-0001 -H 'X-Tenant: t1'

# 中断后续跑：只处理尚未处理的行
curl -s -X POST localhost:8000/order-imports/imp-0001/resume -H 'X-Tenant: t1'

# 受理出入库单（direction 仅 in/out，quantity 为正整数；同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/stock-movements -H 'X-Tenant: t1' -H 'Idempotency-Key: stm-0001' \
  -H 'Content-Type: application/json' \
  -d '{"movement_id":"m1","order_id":"o1","direction":"in","quantity":120}'

# 按标识读取出入库单（返回含 status）
curl -s localhost:8000/stock-movements/m1 -H 'X-Tenant: t1'

# 撤销（冲正）出入库单：置为终态 reversed，不再计入数量汇总（同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/stock-movements/m1/reverse \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: stm-rev-0001'

# 条件检索：某订单的出库单、数量 10..200、时间区间含端点（过滤条件任意组合）
curl -s 'localhost:8000/stock-movements?order_id=o1&direction=out&quantity_min=10&quantity_max=200&limit=50' \
  -H 'X-Tenant: t1'

# 只看已撤销单据 / 返回全部状态（两者互斥，不得同时给出）
curl -s 'localhost:8000/stock-movements?status=reversed' -H 'X-Tenant: t1'
curl -s 'localhost:8000/stock-movements?include_reversed=true' -H 'X-Tenant: t1'

# 游标续页：把上一页返回的 next_cursor 原样传回
curl -s 'localhost:8000/stock-movements?order_id=o1&limit=50&cursor=<上一页next_cursor>' -H 'X-Tenant: t1'
```
- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入在提交请求内同步逐行执行，适合小批量任务（大任务可中断后续跑）。
- 收款只支持整单登记，未实现分期。
- 退款受理后为待处理（`pending`），由结算推进（`POST /settlements` + `/advance`）原子推进为已生效（`effective`）；服务自身不做自动结算。
