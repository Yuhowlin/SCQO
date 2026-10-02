# 停泊實驗（flux parking）：規格

> **狀態（2026-09-26 晚）：已落地。** `qubit_ramsey_flux_pulse` 在四個 repo 都已 commit（scqat `f352fae`、
> SCQO `7435f78`、scqo-qm `f454c7d`、scqo-qblox `edc311e`），procedure 在
> `procedures/qubit-frequency-park`，release fragment 是 `RELEASES.d/qubit-ramsey-flux-pulse.toml`。
> QM 已在 5Q4C q1 上機驗證（§4.13），Qblox 的硬體驗證還欠著（BACKLOG「Hardware validation owed」）。
> 本文件保留為設計紀錄。

> 未追蹤的計畫文件。依據是 2026-09-26 在 5Q4C / cd2 / qm_5q 上的硬體量測。
> §1–§3 是交接時就有的證據。§4 是**規格**，已納入使用者 2026-09-26 的三點修正：
> 磁通視窗改成 start/end、實驗改用 pulse frame、三種量法互補而不是由單一實驗負責。
> §5 只保留「能不能長到 coupler 上」的結論；coupler 的資料與討論在 `docs/coupler-readout-plan.md`，
> 由另一個 session 處理。

## 1. 為什麼要做

2026-09-26 在 5Q4C 上量到：自 09-22 起**三條 z 線的 DC flux offset 全都漂了 +6 ~ +10 mV**，
而每顆的 `f_q_max` 只動了 24 ~ 461 kHz。**offset 會走，apex 不會走。** 重新停回 apex 之後，
q3 的 T1 從 16.8 µs 回到 24.8 µs（+48%），T2\* 從 9.0 µs 回到 10.4 µs。停泊是效能問題，不只是
校準方便與否。

現有的兩個工具都能給出 offset，但都不直接回答「給我一個頻率，該下多少磁通」，而且精度到不了
kHz 級：

- `qubit_spectroscopy_flux_pulse`：pulse frame，給 arch 與 apex，受線寬限制；QM 上還有
  BACKLOG **I20**（z 脈衝播成 4 倍長）。
- `resonator_spectroscopy_flux`：DC frame，看的是共振腔，換成 qubit 頻率要靠色散模型，那個 fit
  釘不住 `f_q_max`。

§4.0 會把這兩個跟新實驗並列成三種互補的量法。

## 2. 手動方法（DC 版，驗證了 reading）

腳本在 `scqat/temp/park_scan.py`（gitignored）。每顆 5 個磁通點加 1 個重複點，約 3 分鐘：

1. `scqo set z<n>.idle_flux=<v> --yes`（或簡寫 `q<n>.idle_flux`），也就是 DC park。
2. `qubit_ramsey --targets <單一顆>`，虛擬 detuning 4 MHz、`max_idle_time_ns=4000`、201 點。
3. **用 periodogram 直接從原始 trace 讀 fringe**，不用 estimator 回報的頻率（見 §3）。
   `drop = detuning − fringe`，所以 apex 是 fringe 最大的地方。
4. 點的順序用 ping-pong（中心、−1、+1、−2、+2），最後再量一次中心點。這樣漂移不會被誤讀成
   曲率，重複點也順便給出重現性：**2–3 kHz**。
5. 用拋物線擬合取頂點。曲率 0.015–0.018 MHz/mV²，離頂點 1 mV 只差約 15 kHz，所以
   **停在 ±1 mV 內就夠**。

這個方法驗證的是 reading（Ramsey fringe + periodogram + 局部二次）。規格**沒有沿用 DC**，原因
見 §4.1。

| | 最終 park (V) | 曲率 (MHz/mV²) | 頂點相對 pulse arch | f01 (MHz) |
|---|---|---|---|---|
| q1 | 0.261019 | −0.0152 | −0.80 mV | 5144.596 |
| q2 | −0.017872 | −0.0183 | −0.79 mV | 4842.148 |
| q3 | 0.007432 | −0.0172 | −0.64 mV | 5191.926 |

## 3. 兩個會給出「看起來合理但錯誤」頻率的坑

1. **折疊。** fringe = |detuning + (f_q − f_drive)|。qubit 往下走得比虛擬 detuning 還遠時，
   fringe 會穿過零，讀出來的頻率就翻號。實例：用 1 MHz detuning 掃 1.4 MHz 的擺幅，得到一條
   上下跳 ±1 MHz 的假 arch。q2 先前「重新停泊卻不動」的謎團就是這個。
2. **estimator 在大 detuning 下會錯。** 4 MHz 時，scqat 的 `ramsey` 回報的頻率錯超過 1 MHz，
   T2\* 給出 0.95 µs 與 62 µs，但原始資料很乾淨（對比度 0.90，periodogram 重現到 3 kHz）。
   0.3 MHz 配 4 µs 視窗時，它至少會老實地回 `outcomes: failed`。**任何消費 `fit` 的程式都要
   先讀 `outcomes`。**

---

## 4. 規格

### 4.0 同一個物理量，三種量法（修正 3）

`flux_offset`、`flux_per_phi0`、`f_q_max_hz`，以及停泊點 `idle_flux`，都有三個實驗能量，各有長短。
**三者都可以提議 `idle_flux`，沒有哪一個是唯一的來源。** 對某個樣品用哪一個、用到哪一步，由
procedure（§4.12）或操作者依樣品決定。

| | `resonator_spectroscopy_flux` | `qubit_spectroscopy_flux_pulse` | `qubit_ramsey_flux_pulse`（新） |
|---|---|---|---|
| frame | DC（絕對） | pulse（相對 idle） | pulse（相對 idle） |
| 看的是 | 共振腔 dip | qubit 光譜線 | qubit Ramsey fringe |
| 給出 | offset、**週期**（寬視窗，可跨數個週期）、粗的 f_q_max 與 g | 整條 arch：offset、週期、`ej_sum_hz`、`f_q_max_hz` | 局部：apex 或指定頻率的停泊點（約 0.1 mV）、f01（約 3 kHz）、曲率 |
| 什麼時候還能用 | qubit 看不太到、T1 很短、剛 bring-up（只需要 readout） | qubit 看得到光譜就行，T2\* 短也可以 | 需要 T2\* 夠長，fringe 在衰減前能走好幾個週期 |
| 弱點 | 色散模型釘不住 `f_q_max`，g 有系統誤差 | 受線寬限制；I20；偏移量有約 10% 的偏差（I25） | 只看局部，需要先有視窗與 arch 預測；有折疊風險；需要 x90 已校準 |
| 提供給誰 | 另外兩個實驗的視窗與 arch 預測 | Ramsey 版的視窗中心與折疊預測 | — |

連帶要改的地方（實作時做，不在本規格範圍）：`qubit_spectroscopy_flux_pulse` 的描述裡寫著
「AUTHORITY for idle_flux」，`resonator_spectroscopy_flux` 的寫著「BRING-UP seed … the
authority for that knob」。這種上下級的說法要改寫成各自的長處與短處。

### 4.1 名稱與 frame：`qubit_ramsey_flux_pulse`（修正 2）

**不用 DC。** DC 改的是 qubit 的靜置頻率：兩個 x90 和 readout 都會落在移動後的頻率上，而 x90
的參數（振幅、DRAG、頻率）是在 idle 點校準的，所以會跑掉。擺幅一大就沒有 Ramsey 可言，park
模式更是如此。pulse frame 讓 **x90 和 readout 一律在 idle 點**，只有自由演化期間用 z 脈衝把
qubit 移開。

- 視窗是**相對 idle_flux** 的脈衝振幅，所以名稱**必須**以 `_pulse` 結尾（有
  `test_flux_pulse_names_carry_the_suffix` 在守）。capability：`flux`、`flux_pulse`、
  `state_readout`、`qubit_reset`。
- 和 `qubit_ramsey_cryoscope` 的關係：同樣是 Ramsey 裡夾一個 z 脈衝，但 cryoscope 的軸是
  （脈衝長度 × 關閉脈衝的 frame），振幅固定，量的是 step response；這裡的軸是（振幅 × τ），量
  的是 arch。形狀不同、模型不同，所以是兩個實驗。driver 端可以共用 probe 模組。

pulse frame 的代價，要一起記下來：

- **答案落在 pulse frame。** 真正的停泊是 DC：`idle_flux(新) = old_idle + a*`。**2026-09-26 在
  q1 上用這個 reading 預試過（§4.11）**：pulse 送到 qubit 的磁通是 DC 的 **g ≈ 0.96**，而且沒有
  常數偏移。所以每跑一次的殘差約是移動量的 4%：移 8 mV 剩 0.3 mV，一輪就進 ±1 mV；移 36 mV
  （q1 停到 `f_q_max` − 20 MHz）剩約 1.5 mV，第二輪才收斂。光譜 probe 量到的 8–15%（I25）比
  這個大，那個 probe 還有 I20 的問題，兩者不必相同。
- **z 脈衝的暫態。** 讀的是相位對 τ 的斜率，所以脈衝上升、下降造成的固定相位不影響結果。但如果
  有 µs 級的長尾，相位對 τ 會彎，要看殘差；cryoscope 量到的 predistortion taps 在這裡也有關係。
- **脈衝形狀**必須是方波 `const`：FlatTopCosine 在 `duration=` 下會補零，而不是拉長。長度要用明確
  的 `// 4` 換成 clock cycle（I20 的教訓）。τ 從 16 ns 起，走 4 ns 網格。

### 4.2 Parameters

Mixins：`TargetSelection`、`AveragingParameters`、`StateReadoutParameters`、
`QubitResetParameters`、`FluxPulseSweepParameters`（相對 frame）、
`_flux_component.FluxComponentParameters`（greenfield 版，kind-agnostic）。

| 欄位 | 型別 / 預設 | 意義 |
|---|---|---|
| `start_flux_v` / `end_flux_v` | float（見 §4.2.1） | 相對 `idle_flux` 的脈衝振幅，**依 start → end 的順序掃**。apex 模式建議 ∓12 mV，off-apex 建議 ∓3–5 mV |
| `num_flux_points` | int = 7，`gt=4` | 局部二次擬合至少要 5 個有效點 |
| `park_frequency_hz` | `float \| None` = None | target 要停在的 f01。**None = 找 flux apex** |
| `flux_side` | `Literal["nearest","lower","upper"]` = `"nearest"` | 視窗同時跨過 apex、兩側都有解時選哪一個：`lower`/`upper` = 電壓比 apex 低/高的那個解，`nearest` = 離目前 idle 近的那個。只有兩側都有解時才會用到 |
| `frequency_detuning_hz` | float = 4e6，`gt=0` | 虛擬 detuning 的**大小**（和 `qubit_ramsey` 同名）；**正負號由實驗決定**（§4.4） |
| `min_idle_time_ns` / `max_idle_time_ns` | 16 / 4000 | τ = z 脈衝的長度 |
| `num_idle_points` | int = 201 | 不叫 `num_points`：這是 2D 實驗，要說清楚是哪一軸 |
| `flux_buffer_ns` | int = 20，`ge=0` | z 脈衝前後各留一段、停在 idle 的空檔，讓兩個 x90 都不碰到脈衝的邊緣；兩端 driver 都照做 |
| `num_averages` | 200 | |

欄位名的關卡已經過了（§5）：target 永遠就是被量的那顆 qubit，所以 `park_frequency_hz` 不會
產生歧義，也不需要「誰是探針」這個欄位。

#### 4.2.1 start / end 取代 min / max（修正 1，整個 capability 一起改）

- 在 `FluxSweepParameters` 上改名，兩個 frame 一起改：`min_flux_v` → `start_flux_v`、
  `max_flux_v` → `end_flux_v`，敘述常數也一起改。不留 alias（沒有向後相容）；舊名字會被
  `extra="forbid"` 大聲擋下來。
- **語意是掃描的順序。** 連續兩次量測在理論上彼此獨立，但實際上有時不是（加熱、長尾、磁滯），
  所以要看得出掃的方向。probe 在每一輪 averaging 裡依 start → end 的順序走；**dataset 的
  `flux_bias_v` 座標保留實際掃描順序，不重新排序**，資料本身就能看出發生了什麼。
- **detuning 一起改成順序語意**（使用者 2026-09-26 決定）。detuning 現在的 `start_*`/`end_*`
  只定義視窗、軸被正規化成遞增，原因是 scqat 的 `peak_fit` 讀遞減軸時會悄悄擬錯（它自己的 module
  docstring 也寫了，真的需要方向時得先修掉那兩個 bug）。
- **amplitude 也改**（使用者 2026-09-26 決定）：`min_amp_factor`/`max_amp_factor` 改成
  `start_amp_factor`/`end_amp_factor`。`_window_ordered` 這個 validator 目前要求 min < max，要改成
  只拒絕零寬度；原本的上下界（≥ 0、< 2）兩端都要檢查。carrier 有 `qubit_power_rabi`、
  `readout_power`、`qubit_resonator_stark`、`qubit_pi_pulse_error`、
  `qubit_deterministic_benchmarking`（會覆寫 `amp_values()` 的 carrier 也要照順序走）；兩個
  driver 的 `_amp_limits.py` 以及一批 Qblox 測試會直接用到這兩個欄位名。
- 之後 flux、detuning、amplitude 三個 capability 的 start/end 都表示掃描順序。
- **順序不能影響 estimator**（使用者 2026-09-26）。同一份資料不管依哪個方向掃，estimator 都要給出
  同樣的結果。要先修好 scqat 已知的兩個 bug：`tools/peak_fit.py:289` 的
  `gamma_max = detuning[-1] - detuning[0]`，以及 `tools/fit_notch_circle.py:155/162/185` 同樣的寫法。
  然後逐一稽核用到 `np.interp` / `searchsorted` / `np.gradient` 的 estimator 與 tool（`np.interp`
  碰到遞減的 xp 會悄悄算錯）。每個讀 flux 或 detuning 軸的 estimator 都要補一個「遞減軸與遞增軸
  結果相同」的測試。
- **獨立成一個 feature（F25），在 F23 之前做，開新 session。** 施工順序：scqat（order-agnostic）→
  SCQO（兩個 capability 的語意與改名、拿掉 detuning 的正規化）→ drivers（兩端都已驗證能掃遞減軸，
  見 detuning 的 docstring）。
- 影響範圍：四個 carrier（`qubit_spectroscopy_flux_pulse`、`resonator_spectroscopy_flux`、
  `qubit_echo_flux_pulse`、`qubit_relaxation_flux_pulse`；後兩個有用名字覆寫預設值），
  `tests/test_capabilities.py`，`scqo-qblox/tests/test_flux_limits.py`，以及任何還在用舊名字的
  `~/.scqo/parameters.toml` 或 campaign plan。這是共用 mixin，要跑完整測試。
  `pair_zz_coupler` 的 `min_coupler_v`/`max_coupler_v` 不在這個 capability 上，但為了一致也一起改
  （跟 I26 的 frame 修正一起做）。

### 4.3 Probe（兩個 driver 必須實現同一個序列）

- 每一發：reset（`reset_method`）→ x90（idle）→ `flux_buffer_ns` → z 脈衝（方波，相對 idle 的
  振幅 a，長度 τ）→ `flux_buffer_ns` → 帶虛擬相位斜率的 x90（相位正比於 τ，用有號的
  `ramp_detuning_hz`，§4.4）→ readout（idle）。
- 迴圈：**averages（外）→ flux（中，start → end）→ τ（內）**。averages 放最外層，漂移會平均分到
  每個磁通點。
- 被掃的元件：預設是 target 自己的 z；設了 `flux_component` 就換成那個 entity 的元件。
  **實作（2026-09-26）**：QM 只接受 qubit 的 z 當來源，coupler 依名稱拒絕
  （`flux_source_name(..., qubit_only=True)`；coupler 的版本留給 F24）。Qblox 目前對任何
  `flux_component` 都依名稱拒絕。
- 兩端的 parity 要一致：順序、空檔、方波，以及同一個有號 detuning（由中立 helper 推導一次）。
  **實作**：兩端開頭都用 y90（Qblox 是 `Rxy(90, φ=90)`）。Qblox 自己的 `qubit_ramsey` 開頭是 X90，
  那是既有的落差，不在這次範圍內。
- `reset_method`：QM 跟 `qubit_ramsey` 一樣開放 active reset；Qblox 依該 repo 的慣例，在上機驗證
  之前維持拒絕（census 已更新）。
- 沒有另外抽出共用的 `_ramsey.py` 模組：兩端的新 probe 都是自己一個模組。

### 4.4 折疊：由實驗決定斜率方向，會出事就事先拒絕

fringe = |s·A + Δf(a)|，其中 A = `frequency_detuning_hz`、s = ±1，Δf(a) 是 z 脈衝期間 qubit 相對
drive 的頻率偏移。只要 s·A + Δf 在整個視窗內不變號，就不會折疊。

- **define_sweep 階段**（還沒佔用儀器）預測整個視窗的 Δf 範圍：用 arch facts（`f_q_max_hz`、
  `flux_offset`、`flux_per_phi0`，以及 `ec_hz`，沒有的話用 0.2 GHz），配上目前的 `idle_flux` 和
  `drive_freq_hz`，算 Δf(a) = f(idle + a) − f_drive。**這些 facts 正是 §4.0 另外兩個實驗提供的。**
- 選擇 s，讓**擺幅比較大的那一側把 fringe 推離零**。apex 模式下 qubit 停在 apex，脈衝只會把它
  往下拉，所以 s = −1、fringe = A + drop，**多大的擺幅都不會折疊**，只受 Nyquist 限制。
- 拒絕條件（依名稱拒絕，並附上可行的 A 或時間網格）：`folding_risk`：A < 1.5 × 較小一側的
  |Δf|；`undersampled`：A + 最大 |Δf| > 0.8 × Nyquist（Nyquist 由實際的 τ 網格算；park 模式擺幅
  大時，就把 τ 的步距縮到 4 ns）。
- **arch facts 缺漏時不拒絕**（實作時改的，依修正 3：粗量法提供的是參考，不是硬性前提）：改用
  apex 情境的預設方向（s = −1；park 模式則依目標在 drive 的哪一側決定），並在 fit 裡記下
  `ramp_sign_from = "default"`；這時的把關是事後的 `fold_suspected`。有 facts 時記為 `"facts"`。
- 有號的 `ramp_detuning_hz` 用 `note_acquisition` 寫進 dataset，estimate() 從那裡讀。
- 設了 `flux_component` 時無法預測受害者的擺幅，所以用預設方向（受害者通常停在自己的 apex 上，
  只會往下掉，所以是 s = −1），不做事前拒絕，只看事後旗標。

### 4.5 Estimator（scqat，新增，1:1 綁定）：`qubit_ramsey_flux_pulse`

reading：資料形狀 (target, `flux_bias_v`, `idle_time_ns`) × {I,Q | population}。模型是「每個
振幅一個 fringe 頻率 → f01(a) → 局部二次」。**軸的順序可能是遞減的**（§4.2.1），estimator 內部
自己處理。

- **逐點化約**用新的 tool `scqat.tools.fringe_frequency`：去掉 DC 後，在細網格上算 periodogram
  峰值，再以它為種子做一次阻尼正弦最小平方。**擬合結果只有落在 periodogram 峰值 ±半個 bin 內
  才採用**，否則回報 periodogram 的值。輸出 `f`、`f_stderr`、contrast、SNR。這個 tool 同時是
  修 `ramsey` estimator seeding 的途徑（I24），兩者靠 tool 共用，不是共用 binding。
- f01(a) = f_drive + s·(F − A)。
- 有效點的條件：SNR 與 contrast 都過門檻，且 F 離 0 或 Nyquist 超過 2 個 bin。無效點不進擬合；
  有效點數記在 `n_valid_points`，少於 5 就 FAILED。
- **apex 模式**：加權二次擬合 → 頂點 a0 與 f(a0)，stderr 用 delta method。成功條件：曲率顯著
  為負，而且 a0 落在有效點的範圍內。
- **park 模式**：同樣的二次式，解 f(a) = `park_frequency_hz`，只取視窗內的根，再依 `flux_side`
  選。
- 二次式在 ±30 mV 內都成立：transmon arch 的四次項在 ±12 mV 時只有二次項的 1e-4。

### 4.6 Result：`fit[target]` 的鍵

凡是會寫回的值，鍵名就用 catalog 的欄位名。相對與絕對兩個 frame 都要報，照
`qubit_spectroscopy_flux_pulse` 的慣例：

- 相對（這個 run 自己的 frame）：`flux_offset_from_idle`、`park_excursion_v`
- 寫回值（絕對，= `old_idle_flux` + 相對值）：`idle_flux`、`flux_offset`；以及 `f_q_max_hz`、
  `f_01_hz`、`drive_freq_hz`
- 誤差與診斷：`idle_flux_stderr`、`flux_offset_stderr`、`f_01_stderr_hz`、`curvature_hz_per_v2`、
  `df_dflux_hz_per_v`（停泊點的斜率，是對磁通雜訊的敏感度）。`flux_per_phi0_from_curvature` 在
  實作時拿掉了：pulse frame 的曲率帶著 g²（約 0.92），推出來的週期會系統性偏掉。
- 出處：`old_idle_flux`、`old_drive_freq_hz`、`ramp_detuning_hz`、`ramp_sign_from`、`n_valid_points`
- 旗標（0/1）：`apex_not_bracketed`、`park_out_of_window`、`multi_target_context`、`fold_suspected`
- plotdata：逐點的 `f01_hz`、`f01_stderr_hz`、`fringe_hz`、`contrast`、`snr`、`valid`，擬合曲線，
  以及原始 2D fringe 圖（依實際掃描順序）

### 4.7 寫回

只有在**單一 target、沒有 `flux_component`、SUCCESSFUL** 時才提議：

| 模式 | knob | fact |
|---|---|---|
| apex | `q.idle_flux = flux_offset`；`q.drive_freq_hz = f_01_hz` | `q.flux_offset`、`q.f_q_max_hz`、`q.f_01_hz`（= apex 頻率） |
| park | `q.idle_flux`（= old_idle + 根）；`q.drive_freq_hz = f_01_hz` | `q.f_01_hz`；**視窗也包住 apex 時**再加 `flux_offset`、`f_q_max_hz` |

表中的位址是 4.0.0 的簡寫 `q.<field>`，實際的 owner：`idle_flux` 在 q 的 flux 線（5Q4C q1：
`z1.idle_flux`），`drive_freq_hz` 在 drive channel（`xy1.q1`），`flux_offset` 在 flux channel
（`z1.q1`），`f_q_max_hz`、`f_01_hz` 在 mode（`q1`）。

- 提議 `drive_freq_hz`（它是 `f_01_hz` 的合法雙胞胎），就不會重演 `qubit_spectroscopy_flux_pulse`
  接受之後 drive 被晾在舊頻率的問題。
- 不寫 `flux_per_phi0` 和 `ej_sum_hz`：那是另外兩個實驗的強項（§4.0）。
- 只記錄、不寫回的情況：
  - 設了 `flux_component`：這是串擾資料。
  - 多個 target（`multi_target_context=1`）：所有線同時掃，串擾跟掃描索引相關，會偏掉頂點；
    相鄰 qubit 同時做 Ramsey 時 fringe 會帶上 ZZ/2，而 ZZ 又跟 coupler 狀態有關。
  - 非 SUCCESSFUL。
- `|park_excursion_v|` 很大時，寫回值會帶著 I25 的比例誤差（§4.1）。procedure 的做法是先接受，
  再從新的停泊點重跑。
- 在有 coupler 的晶片上，`flux_offset` 和 `f_q_max_hz` 都是**在目前 coupler 偏壓下**量到的
  （§5 第 4 點），experiment 的描述裡要寫明。

### 4.8 事前拒絕（在 define_sweep / validate_targets，還沒佔用儀器）

1. 給了 `park_frequency_hz` 又有多個 target：拒絕（決定性的停泊一律單 target）。
2. `park_frequency_hz` 配上 `flux_component`：拒絕（只記錄的 run 不可能停泊）。
3. `park_frequency_hz` > `f_q_max_hz` + 0.5 MHz：拒絕（到不了）。
4. §4.4 的 `folding_risk`、`undersampled`，以及 arch facts 缺漏。
5. coupler 當 target 不必另外處理：它沒有 drive 和 readout channel，`required_operations =
   ("rx","readout","flux_bias")` 會依 channel 存在與否直接擋掉。

### 4.9 pulse 與 DC 的零偏移一致性（已驗證，2026-09-26）

`qubit_spectroscopy_flux_pulse` 從 6–10 mV 外的舊 idle 出發（`20260926-145528-751`）時，量到的
apex 比 DC apex 遠 0.64–0.80 mV（三顆同號，約 10σ）。從 DC apex 出發重跑同一組參數
（`20260926-182548-238`，`--no-update`）時，三顆落在 idle 的 −0.05 / +0.02 / −0.02 mV，都在
stderr 內。所以兩個 frame **在零偏移時一致，誤差跟偏移量成正比**（I25）。這一點支持 §4.1 的
選擇：一旦停好，pulse frame 的再量測和 DC 等價。

### 4.10 硬體驗證清單（5Q4C）

1. **零偏移一致性**（預試已做過一次，§4.11）：正式 probe 跑 apex 模式時，必須**同時**跑 DC
   參考掃描，因為 DC apex 本身每小時會漂 0.2 mV 左右，不能拿幾小時前的數字比。扣掉 g 之後，
   兩者要差在 0.2 mV 以內。
2. **偏移量的比例**（預試已做過一次：g = 0.958 / 0.963）：正式 probe 再量一次，確認跟原型一致。
3. **park 模式**：q1 停到 `f_q_max` − 20 MHz，記錄要迭代幾輪；接受後跑驗證用的 `qubit_ramsey`
   （0.3 MHz / 30 µs），`detuning_error_hz` 要小於 10 kHz。
4. 拒絕路徑都實際走過一次：`folding_risk`、`park_out_of_window`、多 target 的只記錄。
5. 遞減軸：`start_flux_v > end_flux_v` 跑一次，結果要跟遞增的一致。
6. Qblox：probe 做結構性測試（順序、空檔、方波、有號斜率），硬體驗證欠著。

### 4.11 上機預試（q1，2026-09-26 19:51–20:00）

原型 builder 與資料在 `scqat/temp/ramsey_flux_pulse_pretest/`（gitignored）。序列：y90 → 20 ns →
方波 z 脈衝（a, τ）→ 20 ns → x90 → readout；D = −4 MHz（s = −1）；7 個振幅 × 201 個 τ（16–4000 ns）
× 200 次平均，每次約 88 秒。DC 參考是手動方法（run 標記 `flux-park-dc-ref`）。

| run | idle (V) | pulse frame 的 apex（相對 idle） | 高度 | 曲率 (MHz/mV²) |
|---|---|---|---|---|
| A | 0.261019 | −0.288 mV | +12.5 kHz | −0.0141 |
| B | 0.269019（+8 mV） | −8.684 mV | +6.4 kHz | −0.0141 |
| C（A 的重複） | 0.261019 | −0.384 mV | +11.1 kHz | −0.0141 |
| DC 參考 | — | apex 在 **0.260693 V**（−0.326 mV） | +12.8 kHz | −0.0153 |

- **g ≈ 0.96，兩種方法一致**：apex 移動量 8.0 / 8.35 = 0.958；曲率比開根號 √(0.0141/0.0153) =
  0.960。拿 g 回推：A、C 預測 −0.340 mV（實測平均 −0.336），B 預測 −8.69 mV（實測 −8.684）。
- **沒有常數偏移**：扣掉 g 之後的零點正好是 DC apex。A 與「idle 應該在 apex」之間那 0.3 mV，是
  DC apex 自 18:2x 以來**自己漂了 −0.33 mV**（約 0.2 mV/小時）。q1 仍停在 0.261019，這在 ±1 mV
  容許範圍內，沒有改。
- **相位對 τ 是線性的**：每個振幅的 τ 前後半段 fringe 頻率差 ±11 kHz，隨機、跟振幅無關，
  4 µs 的方波脈衝看不出長尾。
- **estimator 的 stderr 太樂觀**：A 和 C 相隔 4 分鐘差了 0.1 mV，但報的 stderr 只有 0.001–0.003 mV。
  中間 B 動過 +8 mV 的 DC，可能是前後次不獨立的例子。procedure 要用 ±0.2 mV 當重現性，不能
  直接用 stderr。
- 高度在 B 低了約 6 kHz：q1 的線動 +8 mV 時，可能串到 q1_q2_c，讓 Lamb shift 變了（推測，尚未驗證）。

### 4.13 四種方法的上機比較（q1，2026-09-26 21:29–21:40，正式 probe）

run 都標記為 `f23-compare`，腳本和記錄在 `scqat/temp/ramsey_flux_pulse_pretest/compare_*`。
流程：DC 參考 #1 → 用 DC 把 q1 移到 apex + 8 mV（0.268756 V）→ 三種方法各自從這個起點找 apex →
DC 參考 #2 → 把 q1 停到 DC 參考 #2 的 apex。coupler 全程固定在 0.16 V。

| 時間 | 方法 | apex (V) | 耗時 |
|---|---|---|---|
| 21:32 | DC 參考 #1（手動，6 次 Ramsey） | **0.260756** | 約 3.5 分 |
| 21:33 | `resonator_spectroscopy_flux`（絕對視窗 0–0.49 V，50 點） | 0.258567 | 16 s |
| 21:33 | `qubit_spectroscopy_flux_pulse`（±40 mV，21 點） | 0.260191 | 53 s |
| 21:34 | `qubit_ramsey_flux_pulse` 第 1 輪（從 +8 mV 出發，`--accept`） | 0.260063 | 97 s |
| 21:36 | `qubit_ramsey_flux_pulse` 第 2 輪（從第 1 輪的停泊點出發） | **0.260432** | 97 s |
| 21:40 | DC 參考 #2 | **0.260448** | 約 3.5 分 |

- **真正的 apex 在這 8 分鐘內移了 −0.31 mV**（#1 → #2），而前面 1.5 小時只漂了 +0.06 mV。中間最大的
  擾動是 21:33 那次 `resonator_spectroscopy_flux`：它把 q1 的 DC 從 0 V 掃到 0.49 V。**大範圍的 DC 掃描
  很可能會讓 apex 有遲滯位移**（推測，尚未單獨驗證），這正是前後次量測不獨立的例子。所以寬視窗的
  量法之後，一定要再做一次細的停泊。
- 如果位移發生在 21:33（最可能的情況），拿 0.26045 當後面三次 run 的真值：
  - `qubit_ramsey_flux_pulse` 第 1 輪：讀到的偏移量 −8.69 mV，真值 −8.31 mV，**g = 0.956**，跟預試的 0.958
    一致。
  - 第 2 輪：**跟 DC 參考 #2 只差 0.016 mV**，兩輪就收斂。
  - `qubit_spectroscopy_flux_pulse`：差 −0.26 mV（偏移量讀大 3%），單次就很接近。
  - `resonator_spectroscopy_flux`：差約 −1.9 mV，最快，但只適合粗定位。
- 三者正好符合 §4.0 的分工：resonator 給粗位置和週期（幾秒鐘），pulse 光譜給到 0.3 mV 級，Ramsey 給到
  0.02 mV 級。

### 4.12 配套 procedure `qubit-frequency-park`（實驗上機驗證後才寫進 `procedures/`）

0. **前提**：coupler 的靜置偏壓已經定案。coupler idle 改過超過 15 mV，所有鄰近 qubit 都要重新
   停泊（§5 第 3 點）。
1. offset 或週期不明、qubit 還看不太到的時候：`resonator_spectroscopy_flux`。它的寬視窗 DC 掃描可能讓 apex
   有遲滯位移（§4.13 量到 −0.31 mV），所以之後一定要接細的停泊，而且不能在細停泊之後才跑它。
2. 需要 arch，或目標離現在很遠：`qubit_spectroscopy_flux_pulse`。它給出 Ramsey 版需要的視窗與
   預測；可以接受它的 `idle_flux` 當作粗停泊。**如果樣品的 T2\* 短到 Ramsey fringe 看不清楚
   （fringe 在衰減前走不到幾個週期，例如 < 1 µs），停泊就停在這一步。**
3. 細解：`qubit_ramsey_flux_pulse`，單一 target，7 點；apex 模式 ∓12 mV，off-apex ∓3–5 mV。
   `|park_excursion_v|` 大於約 20 mV（g ≈ 0.96 時，殘差會超過 ±1 mV）就先接受，再重跑一次。
4. 接受後用 `qubit_ramsey`（0.3 MHz / 30 µs）驗證，並微調 `drive_freq_hz`。
5. 多顆 qubit 依序停泊。qubit 線之間的串擾約 1%，一輪就夠；最後用一次多 target run 巡檢。
6. 每個 cooldown 量一次串擾矩陣，包含 coupler 那幾欄（有號的 m 要用 apex 位置量，§6；矩陣的形狀與用法在 coupler 文件的 §4.4 待決定）。

---

## 5. 關卡：能不能長到 coupler 上？——能，qubit 這邊不需要「探針」欄位

證據、讀法與 coupler 的討論都搬到 **`docs/coupler-readout-plan.md`**，交給另一個 session 處理
（使用者 2026-09-26 決定）。這裡只留下 qubit 規格依賴的結論：

1. coupler 的讀法是「在幾個 coupler 偏壓下，各跑一次鄰居的 `qubit_ramsey_flux_pulse` apex 模式」，
   看鄰居 apex 的**高度**。內層的 target 仍然是被量的 qubit，所以 **§4.2 的欄位名可以凍結**，
   不需要「誰是探針」這個欄位。探針的概念放在以 pair 為 target 的姊妹實驗裡。
2. `flux_component` 維持只記錄：用 coupler 當來源時，鄰居頻率的頂點是「串擾進自己 SQUID」和
   「coupler 的 Lamb shift」兩者的混合，拿來寫回一定是錯的。
3. **先停 coupler，再停 qubit。** 實測 q1_q2_c 這條線對 qubit apex 位置的串擾是 5.5%（q1）與
   7.2%（q3，而且 q3 不是它的鄰居）。coupler idle 改超過約 15 mV，qubit 的停泊就會超出 ±1 mV。
4. 在有 coupler 的晶片上，qubit 的 `flux_offset` 和 `f_q_max_hz` 都是「在目前 coupler 偏壓下」量到的
   （coupler 移 −80 mV 時，q1 的 apex 位置移 4.4 mV、高度移 +524 kHz）。experiment 的描述和
   catalog 的說明都要補註。

## 6. 串擾（qubit 這邊）

- qubit 線 → qubit：動 q1 的線 ±12 mV，q2 移 −1.9 kHz/mV、q3 移 −1.5 kHz/mV。當時 q2、q3 還沒停在
  apex 上，所以這是線性項；換算成頂點位移約 0.06 mV，在這顆晶片上可以忽略。
- coupler 線 → qubit：見 §5 第 3 點；詳細數據在 coupler 文件。
- 符號很重要：qubit 停在 apex 上時，用 `flux_component` 掃只給得出 |m|。**有號的 m 要看兩個來源
  偏壓下的 apex 位置。**

## 7. 待歸檔（已寫進 BACKLOG.md）

- F25 掃描視窗 start/end 表示掃描順序：flux、detuning、amplitude 都改；dataset 保留實際順序，
  estimator 不受順序影響（§4.2.1）。在 F23 之前做。
- F23 `qubit_ramsey_flux_pulse`（§4）。
- I24 scqat `ramsey` estimator 在大虛擬 detuning 下回報錯誤的頻率和 T2\*（§3）。
- I25 z 脈衝相對 DC 的比例：pulse frame 讀到的偏移量是 DC 的 1.08–1.15 倍（§4.9）。
- coupler 相關的 F24、I26：見 `docs/coupler-readout-plan.md`。
