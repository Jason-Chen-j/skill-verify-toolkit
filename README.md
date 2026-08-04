# Skill 驗證工具包

驗證 skill 的品質。四道關卡，從結構檢查到發證。只需要 python3 標準庫，免安裝任何套件。

這裡的 skill 指給大型語言模型（LLM）用的工具目錄，內含四樣東西，SKILL.md（給模型的執行規範）、tool.yaml（工具描述與派單資訊）、schemas/（輸入與輸出的欄位規格，JSON Schema 格式）、examples.json（測試案例）。附 `scripts/calc.py` 的 skill，數值由腳本計算，模型負責轉錄。

機測零額度。實測與品質判定由模型執行，預設做法是把工具包交給你的 AI（agent），照包內《測試方法論.md》直接跑，零設定。選配是用腳本自動跑，接 OpenAI 相容伺服器（照 OpenAI 介面格式回應的模型服務）。見「AI 呼叫設定（選配）」一節。

## 四道關卡

1. **機測** — 腳本驗結構與格式，零 LLM 額度。
2. **實測** — 執行工具包的模型照《測試方法論》逐案測試，寫出實測存證。
3. **品質判定** — 獨立模型讀存證，照 `scripts/judge.py` 的評分規則段（檔內標記 RUBRIC）逐案評分，寫出品質判定檔。
4. **發證** — `certify.py` 收斂前三關結果，全過才在 skill 目錄寫入 `certification.json`。

**第 3 關是重點。** 前兩關驗「跑得出東西」，JSON 解析得開、必填欄位齊。這兩件全過，輸出裡的數字仍然可能是錯的。總分對不上明細、單項分數超過滿分、模型呼叫了腳本卻寫自己心算的數字，這些都不會讓 JSON 壞掉，所以前兩關一律放行，靠第 3 關抓。

## 快速上手

1. 取得工具包，`git clone` 或下載整個資料夾。
2. 機測，`python3 run_verify.py <skill目錄或上層目錄> --out-dir 驗證報告`。
3. 實測，把工具包交給執行環境的模型（agent），照包內《測試方法論.md》逐案測，存證寫進報告目錄。
4. 品質判定，換一個模型（或另開 agent），依 `scripts/judge.py` 的評分規則段（檔內標記 RUBRIC）建立 `<skill>_品質判定.json`。
5. 重跑 `python3 run_verify.py <目錄> --mode judge --out-dir 驗證報告`，產出《驗證總覽.md》與《驗證明細.md》。
6. 發證，`python3 scripts/certify.py <目錄> --evidence-dir 驗證報告 --report-dir 驗證報告`。

第 3、4 步的選配：沒有 agent 環境，或要批次自動化時，改用腳本接 OpenAI 相容服務。

```bash
LLM_MODEL=<模型> python3 scripts/run_cases.py <目錄> --out-dir 驗證報告
LLM_MODEL=<另一個模型> python3 scripts/judge_llm.py <目錄> --out-dir 驗證報告
```

設定見「AI 呼叫設定（選配）」。兩條路產出同一種存證與判定格式，後續關卡通吃。

重複驗證時，在第 2 步的指令加上 `--new-run`。工具建立新的結果目錄，同名目錄已存在時依序使用 `_重複驗證_01`、`_重複驗證_02`。執行結果第一行顯示本輪實際使用的目錄，後續存證與判定都使用該目錄。

只跑到第 4 步就收工，等於只驗了「跑得出東西」。

## AI 呼叫設定（選配）

本節只給走腳本自動化的情境。預設做法是模型照《測試方法論.md》直接跑，不需要這裡的任何設定。

`run_cases.py` 與 `judge_llm.py` 走 OpenAI 相容服務的 `/chat/completions` 介面，設定全部來自環境變數。

| 變數 | 用途 |
|---|---|
| `LLM_MODEL` | 模型名稱 |
| `LLM_API_BASE` | 伺服器位址（含 `/v1`） |

兩個變數都必填，沒有預設值。

```bash
LLM_API_BASE=http://localhost:<埠號>/v1 LLM_MODEL=<模型名> \
  python3 scripts/run_cases.py <目錄> --out-dir 驗證報告
```

已知限制：

- 附 `scripts/calc.py` 的 skill 需要模型支援工具呼叫（tool-calling），不支援的模型只能跑純生成 skill。
- 兩支腳本預設要求伺服器照輸出欄位規格回傳（`json_schema` 結構化輸出）。伺服器不支援時加 `--no-schema`，欄位規格的遵循改由第 3 關把關。實測時服務回 400 錯誤，多半是 response.json 不符嚴格模式（strict）要求，也就是 `required` 沒列全、缺 `additionalProperties: false`。這是交付檔的缺陷訊號，完整記在 `api_error` 欄位。
- 閱卷評審用能力強於實測執行者的模型。
- 單次呼叫逾時預設 180 秒（判定 300 秒），`--timeout` 可調。單一案例失敗記 `api_error` 後繼續，不中斷整批。全程輸出同步寫 `<out-dir>/run_cases_全量.log` 與 `judge_llm_全量.log`。

分數的算術由腳本執行。閱卷模型只給五維度分數與問題清單，`total_score`、`average_score`、`verdict` 由 `judge_llm.py` 依 `judge.py` 的同一套規則計算。實測模型與閱卷模型相同時腳本印警告，自己改自己的考卷，判定獨立性不足。

全部產出固定在 `--out-dir` 一個目錄。受測的 skill 目錄保持乾淨，唯一寫入是第 4 關 `certify.py` 產生的 `certification.json`（交付檔的一部分，唯一寫入者是 certify.py，人工不建不改）。全程輸出落在 `驗證報告/run_verify_全量.log`，不截斷。

## 實測與品質判定

一組案例的實測存證包含完整輸出、實際模型名稱與 schema 檢查結果。有 `scripts/calc.py` 的 skill 必須執行腳本取得數值，禁止心算，並把每次呼叫記進存證的 `tool_calls`（判定維度 J5 靠它驗數字有沒有被改掉）。存證 `<skill>_驗證結果.json` 的 `model` 欄位填實際模型名。判定階段將存證統一為 UTF-8、2 格縮排，並在 `response` 欄位呈現可讀的 JSON 回應，`raw_response` 保留模型原始回應供驗證。完整測法與存證格式在包內《測試方法論.md》。

品質判定使用五個 20 分維度，任務完成度、輸入忠實度、規格與風險遵守、可直接使用性、語言與表達。品質判定檔使用 `version: 2`，逐案列出分數、嚴重問題、可忍受問題與可核對的依據。完整規則與欄位規格（含 `criteria` 固定清單）在 `scripts/judge.py` 的評分規則段（檔內標記 RUBRIC）。

金額、法規、期限、資格或當事人資料錯誤屬嚴重問題，直接不通過。字數、頁數、語氣與段落問題屬可忍受問題，依品質分數判定。

輸出語言的標準來自 SKILL.md 本身。SKILL.md 寫明輸出語言（例如「一律以繁體中文輸出」）時，拿實際輸出去對。沒寫，語系由使用該 skill 的平台決定，這一項不判。

## 判定的八個維度（`judge.py`）

| 代號 | 判什麼 |
|---|---|
| J1 | 解析診斷，空輸出、markdown 圍欄（``` 標記）、截斷、輸出了不只一份 JSON |
| J2 | schema 驗證，type、enum、數值範圍、additionalProperties，不只必填欄位 |
| J3 | 總計對組成，`*_total` 對不對得上同層明細加總 |
| J4 | 分類計數，`*_count` 對不對得上明細筆數 |
| J5 | 轉錄忠實度，存證 `tool_calls` 記錄的腳本回傳值，有沒有被原封不動抄進最終輸出 |
| J6 | 輸出語言，只在 SKILL.md 自己宣告時才判 |
| J7 | 存證誠信，`model` 欄位、案例數對不對得上 examples、`raw_response` 非空 |
| J8 | 存證新鮮度，存證比 SKILL.md 或腳本舊，代表講的是上一版 |

品質判定分四級。**可直接交付**（90 至 100 分）、**通過，有可忍受問題**（80 至 89 分）、**需修正**（60 至 79 分）、**不通過**（0 至 59 分或存在嚴重問題）。

「待補驗」與「品質未判定」不是有問題，是還沒被驗到那一層。待補驗＝沒有實測存證。品質未判定＝機械檢查無問題但還沒有品質判定檔。報告逐支列「驗到了什麼、沒驗到什麼」。沒驗到的是盲區清單，要當待辦，不是當結案。

## 判定器自己的對照組

```bash
python3 scripts/judge_selftest.py
```

25 組案例，正例（植入已知缺陷，必須抓到）與反例（長得像缺陷但其實正確，必須放行）各一半。改 `judge.py` 之後一定要跑，全綠才算數。反例是重點。`total_cost_ceiling` 是上限不是加總、`{units, rate, subtotal}` 是算式紀錄不是加項清單，這兩種誤判會讓人去改一份本來就對的檔案，跟漏抓一樣糟。

## 參數

| 參數 | 用途 |
|---|---|
| `--out-dir <目錄>` | 所有產出的固定目錄（預設 `驗證報告`） |
| `--new-run` | 建立新的報告目錄，同名目錄已存在時依序使用 `_重複驗證_01`、`_重複驗證_02` |
| `--mode mech` | 只跑機測 |
| `--mode judge` | 只跑判定（存證已就位時用） |
| `--skip-done` | 報告目錄已有存證的支數，不列入方法論（續跑用） |

## 去識別化設定

去識別化黑名單由執行環境提供。要檢查特定識別資料（公司名稱、統一編號、內部帳號）時，執行前設定 `SKILL_DEID_BLOCKLIST`，以半形逗號分隔禁止字串。

```bash
SKILL_DEID_BLOCKLIST='公司名稱,統一編號,內部帳號' python3 run_verify.py <skill目錄> --mode mech
```

預設行為是跑結構、格式與簡體字檢查，黑名單比對只在設定變數後執行。

## 各腳本（scripts/）

| 腳本 | 驗什麼 | 需要什麼 |
|---|---|---|
| `check_b_skill.py` | 機測。檔案齊全、tool.yaml、常見說法（沒寫時標建議；有寫時驗格式，並抓多支 skill 共用同一句）、examples 欄位名（每組用 `user_query`＋`input`）、簡體、去識別化、撞名。examples input 的內容由品質判定把關 | python3 |
| `check_mcp_skill.py` | 機測，給以 MCP 伺服器構成的 skill（MCP＝Model Context Protocol，讓模型呼叫工具的通訊協定）。驗伺服器啟動、工具清單、逐工具呼叫 | python3 |
| `check_consistency.py` | 輸出數字自相矛盾（吃實測存證檔） | python3＋存證 |
| `regress_calc.py` | calc.py 改動後行為回歸（吃 `驗證結果_工具版.json` 基準） | python3＋存證 |
| `check_routing.py` | 派單觸發語衝突。可攜用法，`python3 scripts/check_routing.py <skills根目錄...>` | python3 |
| `judge.py` | **第 3 關判定**。驗存證、schema、數值與品質判定資料，產出《判定報告.md》，總入口再整合成《驗證總覽.md》與《驗證明細.md》 | python3＋存證＋品質判定檔 |
| `judge_selftest.py` | `judge.py` 的對照組，25 組正反例 | python3 |
| `certify.py` | **第 4 關發證**。機測錯誤（ERROR）0 筆＋存證齊且比交付檔新＋品質判定平均 ≥ 80 且無嚴重問題，全過才在 skill 目錄寫 `certification.json`（見下節）。沒過不寫，已有舊證改寫 `certified: false` 撤銷 | python3＋存證＋品質判定 |
| `format_evidence.py` | 存證檔統一格式（UTF-8、2 格縮排、補 `response` 欄位） | python3 |
| `run_cases.py` | **第 2 關實測**。逐案呼叫模型產出存證；附 calc.py 的 skill 走工具呼叫，腳本實際執行 calc.py 並記 `tool_calls` | python3＋AI 呼叫設定（選配） |
| `judge_llm.py` | **第 3 關閱卷**。呼叫模型逐案評分產出品質判定檔；total_score、average_score、verdict 由腳本計算 | python3＋AI 呼叫設定（選配）＋存證 |
| `llm_client.py` | OpenAI 相容服務的最小用戶端，前兩支共用。純標準庫 | python3 |

## certification.json

發證產物是 skill 目錄內的單一 JSON 檔，自包含、無狀態（不依賴任何外部紀錄）。讀取端看 `certified` 一個欄位就知道通過與否，要驗完整性再重算 `content_hash` 比對。每次發證重新驗全部關卡。

| 欄位 | 意義 |
|---|---|
| `spec_version` | 格式版本號。欄位只增不改，新增欄位時版本號加 1 |
| `skill_id` | skill 資料夾名稱 |
| `skill_type` | 構成分類，工具依目錄構成自動判定。`"A"`＝附計算腳本、`"B"`＝純生成、`"MCP"`＝MCP server |
| `certified` | 是否通過全部關卡 |
| `score` | 品質評分平均（0–100）。`certified` 為 `true` 時 ≥ 80，無評分紀錄時 `null` |
| `tested_model` | 實測時實際執行的模型名稱。skill 行為與模型相關，讀者據此評估證據效力 |
| `certified_at` | 認證時間，ISO 8601 格式（國際標準日期時間寫法）含時區 |
| `content_hash` | 交付檔內容雜湊（sha256）。改任何交付檔，重算就對不上，認證即失效 |

## 收案標準

機測錯誤（ERROR）0 筆只是第一關。收案級驗證＝實測多案例＋完整存證＋品質判定。

`judge.py` 判機器判得準的東西。腳本判不了的規則在 `scripts/judge.py` 的評分規則段（檔內標記 RUBRIC），由品質判定模型逐案讀 SKILL.md 與 `raw_response` 去判。

- 法規引用要標滿五件，法規全名、條號、最後修正日、生效狀態、**條文原文**。
- 內建參考值要有覆寫欄位、版本日期、可操作的揭露警語。
- SKILL.md 自己列的禁止行為有沒有被踩到。
- 日期要驗兩件，起算點、期間長度。

## 授權

GPL-3.0，見 LICENSE。使用與修改自由；散布修改版必須同樣以 GPL-3.0 開源。
Copyright (c) 2026 Jason Chen
