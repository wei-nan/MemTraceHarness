# Goal Plan Smoke：Skill 用途、權限與 Goal Check 機制

本文檔說明 MemTraceHarness 分階段（phased）執行架構中的三項核心階段技能（stage skills：`plan`、`review`、`develop`）之用途與權限，以及 Controller 的 `goal_check` 在何種情況下會阻擋任務被標記為「完成」。

## 1. 核心階段技能（Stage Skills）之用途與權限

在 MemTraceHarness 的設計中，**Controller 是唯一的角色（role）**，其餘階段執行單元皆為 Controller 調用的**技能（skills）**。每個技能透過獨立、全新的 CLI 呼叫執行，並以結構化產出（artifact）交換資訊，維持上下文邊界與審查的獨立性。

### 1.1 `plan`

- **用途（Purpose）**：
  - 為目標的單一階段（phase）起草或修訂實作計畫，將該階段目標轉化為 `develop` 技能可執行的具體規格。
  - 產出包含：預期影響檔案（affected files）、分級驗收條件（tiered acceptance criteria：以 `[必要]` 與 `[建議]` 分級）、範圍排除項（scope exclusions）、待釐清問題（open questions）與回滾方案（rollback）。
  - 支援三種模式：
    - `plan`：常規規劃模式。
    - `revision`：根據 G1 審查提出的阻擋問題（blocking findings）進行計畫修正，不擴增非必要範圍。
    - `escalation`：當 `review` 技能發現規劃出現推理缺口時，升級以更高推理能力模型進行重新規劃。
  - 此階段不對儲存庫做任何實質修改。
- **權限（Permission）**：
  - `read-only`（唯讀）。不可寫入儲存庫或工作目錄。

### 1.2 `review`

- **用途（Purpose）**：
  - 獨立評審計畫（G1）或開發產出（G2），在乾淨且獨立的上下文（fresh context）中評估並回傳 `PASS`、`REJECT` 或 `NEEDS_HUMAN` 裁決。
  - 永遠以全新呼叫進行，只接收前一階段的 artifacts（例如 plan 產出、develop 實作摘要與測試證據、Harness 自動驗證輸出），絕不讀取其他技能的原始對話歷史，藉此確保評審獨立性。
  - 支援兩種模式：
    - `g1`（計劃門檻）：審查計畫本身的完整性、驗收條件是否可觀測、方案可行性與風險；此階段程式碼與測試尚不存在為正常現象，不可視為缺口。
    - `g2`（實作門檻）：在開發完成後，對照已核准計畫及其驗收條件，審查 `develop` 技能的實作成果、變更內容與測試證據。
  - **預設通過（PASS by default）原則**：僅在發生安全性或防護違規（如暴露金鑰／憑證／個資、未授權外部副作用、破壞性或不可逆操作、讀寫 off-limits 目錄、繞過防護控制等，使用 `reason_code: security`）時回傳 `REJECT`；一般邊界案例遺漏、風格或文字細節則列為 low/medium 或 defer finding 供參考，不阻擋通過。
- **權限（Permission）**：
  - `read-only`（唯讀）。不可寫入儲存庫或工作目錄。

### 1.3 `develop`

- **用途（Purpose）**：
  - 實作已核准的計畫（accepted plan）或 Controller 直接指派的工作項目，在工作目錄中進行檔案修改、編寫程式碼與測試，並誠實回報變更清單（`changed_files`）與測試驗證證據（`tests`）。
  - 支援模式：`develop`、`revision`（依據 G2 或測試回饋修正）、`operate`（維運操作）。
  - 優先落實計畫中的 `[必要]` 條件；若有 G1 傳遞的 defer 備註，僅在成本低且符合需求時順帶處理，其餘列於 gaps。其自報的完成狀態（status: completed）不等於最終結論，必須由 G2 審查與 Harness 自動驗證確認。
- **權限（Permission）**：
  - `workspace-write`（工作目錄寫入）。是系統中**唯一**擁有工作目錄寫入權限的技能（Controller 與其他所有技能皆嚴格保持 `read-only`）。

---

## 2. Controller 的 `goal_check` 在什麼情況下會擋下「完成」

在分階段循環中，當最後一個階段完成且 Controller 企圖收斂任務（執行 `finish` 或 `merge`）時，Harness 會依據啟動時制定的目標計畫（`GoalPlan`）中的成功條件（`success_criteria`），嚴格判定 Controller 提交的 `goal_check`。

若判定結果為未完成（`verdict.complete == False`），Harness 會拒絕將任務標記為「完成（succeeded）」，而是強制將任務狀態標記為 `needs_human`（回報「這個任務只完成了一部分，原始目標還沒有達成，所以沒有標成完成。」），並具體列出未達成的問題與項目。

具體而言，Controller 的 `goal_check` 會在以下四種情況下擋下「完成」：

1. **未回報目標完成度（Missing Goal Check）**：
   - 當目標計畫中已設定成功條件（`required = len(plan.success_criteria) > 0`）時，若 Controller 在結束時未回報 `goal_check` 物件（或傳入非字典型態），會被判定為「Controller 沒有逐項回報目標的完成度」而擋下。
   - （註：僅在未定義任何成功條件的舊版或預設單階段計畫中，才允許省略 `goal_check`）。

2. **目標完成度格式不符合規範（Malformed Format）**：
   - `goal_check` 的整體裁決（`verdict`）不在合法值清單（`"met"`, `"partly_met"`, `"not_met"`）之內。
   - 條件清單（`criteria`）不是陣列（list）。
   - `criteria` 中的任何項目不是物件，或者其狀態（`status`）不在合法狀態（`"met"`, `"unmet"`, `"unverified"`）之內。
   - 以上格式錯誤皆會被判定為「Controller 回報的目標完成度格式不對」而擋下。

3. **回報條件數量未涵蓋所有成功條件（Incomplete Criteria Coverage）**：
   - Controller 回報的 `criteria` 數量（`answered`）小於目標計畫所要求的成功條件總數（`required`）。
   - 會被判定為「目標有 X 個成功條件，Controller 只回報了 Y 個」而擋下（不可跳過任何一項）。

4. **存在未達成條件、未收尾項目或整體裁決非完全達成（Unmet Criteria / Non-met Verdict / Remaining Items）**：
   - 整體裁決 `verdict` 不等於 `"met"`（例如為 `"partly_met"` 或 `"not_met"`）。
   - 即使 Controller 自行宣告 `verdict == "met"`，但只要 `criteria` 清單中仍有任一條件的狀態為 `"unmet"`，宣告即無效。
   - `goal_check` 中包含 `remaining` 清單且內含任何未完成事項（這些項目會自動被併入 `unmet` 計算）。
   - 只要有上述任一情況，判定問題即為「目標還沒有完全達成」並擋下完成。

（附註：若某些條件狀態為 `"unverified"`（未獨立驗證），只要 `verdict == "met"` 且無 `unmet`，任務仍可結束完成，但 Harness 會在最終報告中明確提示使用者該條件尚未經過獨立驗證，需由人工自行確認。）
