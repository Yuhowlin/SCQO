# coupler 狀態讀取：三個基礎工具實驗的設計

> 計畫文件（2026-09-27）。內容只有三個直接讀 coupler 的實驗該怎麼設計；其餘相關工作記在 BACKLOG F24。
> §3 實驗 1 已於 2026-09-27 實作並上機（5Q4C q1_q2、q2_q3，只有 QM 版）；§4 實驗 3 已於同日實作並上機（只有
> QM 版，estimator v2 也上機驗證過）；§5 實驗 2 的規格已核可（π 先用 x180）。
> 驗證用的腳本與原始結果在 `scqat/temp/coupler_scan/`（gitignored），run 標記為 `coupler-scan`。

## 1. 三個實驗

1. **coupler 的 flux 週期**：用已知頻率驅動 qubit，同時掃 coupler flux。coupler 靠近 qubit 時，qubit 會被推開、
   打不上去，從打不上去的位置推出週期。
2. **coupler 頻率 by ZZ**：掃 coupler tone 的頻率，打完之後對 qubit 打 π 再讀取。coupler 被激發時，ZZ 會讓
   π 打偏。
3. **coupler 頻率 by swap**：掃 coupler tone 的頻率，打完之後用慢速 ramp 讓 coupler 與 qubit 絕熱交叉，
   coupler 的激發因此轉到 qubit 身上。

順序是 **1 → 3 → 2**：實驗 1 給出實驗 3 的 ramp 目標；實驗 3 找到 f_c 之後，才能決定實驗 2 的 port 要怎麼設。

## 2. 5Q4C 上的硬體事實（2026-09-27 從 state.json / wiring.json 讀出）

- **coupler 線**：LF-FEM analog 輸出，`direct` 模式，**±0.5 V**。q1_q2_c 的 `decouple_offset` 是 0.16 V，
  q2_q3_c 是 0.06 V，q3_q4_c 與 q4_q5_c 是 0 V。`const` 振幅 0.25 V，QUA 的 `amplitude_scale` 必須小於 2，
  所以 |b| < 0.5 V。q1_q2_c 的脈衝 b 只能在 **−0.5 … +0.34 V**（線上電壓 −0.34 … +0.5 V）。
- **q1 的 xy**：MW-FEM `con1/6/2`，band 1，LO 4.9 GHz。q2 的 xy 是 `con1/6/3`，跟 q1 同一組 port pair，
  共用 band。band 1 的 LO 範圍是 0.05–5.5 GHz，band 2 是 4.5–7.5 GHz，band 3 是 6.5–10.5 GHz。
- **idle 時 coupler 在 qubit 上方**（q1_q2_c）。證據有兩個：0.16 V 是 J=0 的點，而這種抵消只在 coupler 高於
  qubit 時才會發生；另外 coupler 電壓越高，q1 被往下推得越快（§6.1：0.20 / 0.22 / 0.24 V 時分別是
  −1.1 / −2.4 / −5.5 MHz）。用極點外推，**q1 的交叉點約在 0.27 V（DC）**；以 coupler apex 0.074 V（§6.1）為
  中心，鏡像約在 −0.12 V。
- **成員頻率**：q1 5.1446 GHz（high）、q2 4.8421 GHz（low）。q1 的 arch 曲率是 −14.8 kHz/mV²；
  q1_q2_c 對 q1 的串擾 m 是 5.45 %（§6.1）。

## 3. 實驗 1：`pair_coupler_crossing_pulse`（暫名，feature `pair-coupler-crossing-pulse`）

**問題**：coupler 在哪些 flux 越過鄰居 qubit？由這些交叉點推出 coupler 的 apex 位置、週期與 f_c_max。

**原理**：探針在 idle 時的頻率 f_q 已知（`drive_freq_hz`）。coupler 越過 f_q 時，兩者混成（分裂 2g），qubit 被推離
f_q，x180 就打不上去。x180 的頻寬約 ±12 MHz，所以只有在交叉點附近才會失效，dip 很乾淨。串擾造成的偏移對它
影響不大：b = −0.28 V 時，q1 自己的 flux 只移了 −15 mV，頻率約 −3.7 MHz。

**target**：一個 `qubit_pair`（依 coupler role 找到 coupler 與它的 flux channel；high/low 兩個成員要有 drive 與
readout）。**一次只准一對**：coupler 線對非鄰居也有 5–7 % 的串擾，兩對同時量會互相污染。

**Parameters**

| 欄位 | 型別 / 預設 | 意義 |
|---|---|---|
| `start_coupler_flux_v` / `end_coupler_flux_v` | −0.45 / +0.30 | coupler 脈衝振幅，**相對 coupler 的 `idle_flux`**，依 start → end 的順序掃。超出 rail 或 `amplitude_scale` 範圍時，driver 依名稱拒絕並報出可用範圍 |
| `num_coupler_flux_points` | 376 | 預設 2 mV 一步（理由見下方「精度」） |
| `measure` | `Literal["both","high","low"]` = `"both"` | 哪些成員打 x180。兩個成員**一律都讀** |
| `coupler_side` | `Literal["auto","above","below"]` = `"auto"` | idle 時 coupler 在探針上方或下方。`auto`：兩個成員都量時由資料判斷（estimator 第 5 步）；只量一個時只報對稱點，不宣稱它是 apex |
| `coupler_ec_hz` | `float \| None` = None | arch 模型用的 coupler Ec。None 表示先用 coupler mode 的 `ec_hz` fact，沒有就用 0.2 GHz；實際用的值記在 `ec_hz_used`。在 5Q4C 的數字下，Ec 從 0.2 改到 0.3 GHz，週期變不到 1 % |
| `flux_buffer_ns` | int = 100（0，或 ≥16 且為 4 的倍數） | coupler 脈衝開始後等多久才打 x180，以及打完後再等多久 coupler 才回 idle。coupler 線沒有 predistortion，要給它時間穩定 |
| `num_averages` | 300 | |
| `reset_method` 等 | `QubitResetParameters` | 同其他實驗 |

需要 state discrimination（輸出是 `joint_population`）。沒有 threshold 時由 driver 依名稱拒絕。

**coupler 視窗做成 capability `coupler_flux`**：`_capabilities/coupler_flux.py` 提供
`CouplerFluxPulseSweepParameters`（上表前三欄）、軸名 `coupler_flux_v`，以及
`coupler_anchor_v(experiment, pair)`（pair → coupler role → coupler flux channel 的 `idle_flux`）。這個視窗
只有相對 frame，所以 carrier 的名稱必須以 `_pulse` 結尾，這條規則寫進 `test_capabilities`；`test_sweep_order`
也要涵蓋它。動到 capability 算共用核心，SCQO 要跑全套測試。

**序列（每一發）**

1. reset 兩個成員。
2. coupler：播 `const × (b / ref)`，長度 T = 2 × `flux_buffer_ns` + x180 長度。
3. 成員：等 `flux_buffer_ns` → 被量的成員同時打 x180（頻率 = 各自的 `drive_freq_hz`）→ 再等 `flux_buffer_ns`。
4. coupler 回到 idle → `align` → 兩個成員都讀取（2-level）→ 存四個 joint indicator。

迴圈順序：averages（外）→ b（內，依 start → end）。以 q1 的 thermalization 估，一次 run 約 30 秒。

已知的系統誤差：b 越過交叉點之後，x180 可能又打得上去；但脈衝結束時 coupler 會快速掃回、再越過一次
交叉點，Landau–Zener 會把一部分激發留在 coupler。所以交叉點外側的基準線可能比內側低，estimator 不能
假設兩側基準線相同。

**contract**：sweeps `("coupler_flux_v",)`，variables `("joint_population",)`，readout_dims
`("joint_state",)`，`target_kinds = ("qubit_pair",)`。

**simulate**：用 stable seed 放一條隱藏的 coupler arch（F、P、x0、Ec、g），成員頻率固定為 5.1 / 4.8 GHz
（simulate 不讀 device 狀態）。用兩能階模型算每個 b 下 qubit 被推開的量 δ，用 Rabi 公式算 x180 的激發率，
再乘出 joint 分佈並加雜訊。放的時候確保兩對交叉點都在視窗內。

**estimator（scqat，新的，1:1）：`pair_coupler_crossing`**

1. 進入時 `ascending(dataset, "coupler_flux_v")`，並有 `order_free` 測試。
2. 從 joint 分佈算邊際：P_high = P10 + P11，P_low = P01 + P11。
3. 每個被量的成員：基準線取高分位數（例如第 90 百分位），正規化 s = P / 基準線，s < `dip_threshold`
   （預設 0.5）的連續區段算一個 dip。dip 中心取 (1 − s) 的加權質心，並報寬度與最低值。
4. 配對：取夾住 b = 0（idle）的最近兩個 dip，左右各一，得到對稱點 c_k 與半距 d_k。夾不住就設
   `crossings_not_bracketed_<role>`。
5. 兩個成員都量時：
   - 兩個對稱點要一致，差太多就設 `center_mismatch`。
   - **判斷方向**：高頻成員的交叉點在內圈 → idle 時 coupler 在上方，中心是 apex；低頻成員在內圈 →
     coupler 在下方，中心是 anti-apex。跟 `coupler_side` 指定的方向矛盾時設 `side_conflict`。
   - **arch 模型**：f_c(x) = (F + Ec)·√|cos(π(x − x0)/P)| − Ec。中心是 apex 時 cos(π d_k / P) =
     ((f_k + Ec)/(F + Ec))²；中心是 anti-apex 時把 cos 換成 sin。兩個方程式解 F 與 P：d_high/d_low 是 F 的
     單調函數，先一維求根得到 F，再算 P。無解時設 `arch_unsolved`。誤差用數值 Jacobian 傳遞。
6. 只量一個成員時只報對稱點；只有 `coupler_side` 有指定時，才判定它是 apex 還是 anti-apex。

**精度**（以 5Q4C 的數字估：q1 半距 0.196 V，兩顆相差 300 MHz）：兩個半距只差 4–14 %，所以結果是「估計」。
5 mV 的步距下 F 約 ±0.6 GHz、P 約 10–15 %；2 mV 時約 ±0.25 GHz、5 %。若 F ≥ 8 GHz，P ≈ 0.5 V，下一個週期的
交叉點會落在可達範圍的邊緣，那時就能直接量到週期。實作後用 5Q4C 的數字做離線合成資料（2 mV、300 次平均）：
F 偏差 < 0.1 GHz、P < 1.5 %，報的 stderr 約 0.07 GHz。這個 stderr 只含取樣與雜訊，不含 g_c、LZ 基準線這類
系統誤差，上機時要以 §6.2 的比對為準。

**Result（`fit[pair]`）**

- 每個成員的交叉點（pulse frame）：`crossing_<role>_lower_v`、`crossing_<role>_upper_v`、dip 寬度與最低值。
  這也是實驗 3 的 ramp 目標。
- `center_from_idle_v`、`center_kind`（`apex` / `anti_apex` / `unknown`）、`old_coupler_idle_flux`。
- 寫回值（絕對）：`flux_offset`（apex）、`flux_per_phi0`、`f_q_max_hz`，各附 stderr；以及 `ec_hz_used`。
- 旗標（0/1）：`crossings_not_bracketed_high/low`、`center_mismatch`、`side_conflict`、`arch_unsolved`。
- SUCCESSFUL：每個被量的成員都夾到一對交叉點，而且沒有 `center_mismatch`、`side_conflict`。
  `arch_unsolved` 不算失敗，只是不提議週期與 f_c_max。

**寫回**（單一 pair、SUCCESSFUL 才提議；**永遠不動 coupler 的 `idle_flux`**）

- `<coupler>_z.flux_offset`：apex 已判定時（兩個成員都量，或 `coupler_side` 有指定）。
- `<coupler>_z.flux_per_phi0`、`<coupler>.f_q_max_hz`：arch 模型有解時。描述要寫明這是依賴模型的估計，
  直接量 f_c 的是實驗 2、3。

**pulse frame 的代價**：交叉點、中心與週期都以 pulse 伏特表示。qubit 線的 pulse/DC 比例 g 約 0.96，coupler 線
的還沒量過；中心離 idle 約 −86 mV，所以絕對的 `flux_offset` 可能偏約 3 mV，週期偏約 4 %。g_c 的量法見 §6.2。

**事前拒絕**：多於一對 target；pair 沒有 coupler，或 coupler 沒有 flux channel；成員缺 drive 或 readout；視窗
零寬度。driver 端：rail 與 `amplitude_scale`（沿用 `check_flux_pulse_relative`）。

**只做 QM 版**：Qblox 那邊沒有帶 coupler 的晶片，Qblox 不註冊這個實驗。

**測試**

- scqat：`tests/test_pair_coupler_crossing_estimator.py`：apex 中心與 anti-apex 中心兩種合成資料都要還原出
  中心、方向、F、P；`order_free`；只量一個成員的路徑；夾不住時報 FAILED；擬合失敗時圖仍要畫得出來。
- SCQO：`test_model_experiments -k crossing`；`test_capabilities` 與 `test_sweep_order` 涵蓋新的
  capability；frame 測試（`flux_offset = old_coupler_idle_flux + 中心`）；simulate → estimate 要還原放進去的值；
  多對 target 被拒絕；`update()` 只提議 fact、從不提議 `idle_flux`。跑 `update_docs.py`，再跑 SCQO 全套。
- scqo-qm：用 live quam_state 建 q1_q2 的程式，確認 QUA 裡有 coupler 的 const 播放、兩個成員的 x180 與
  讀取；超出 rail 時依名稱拒絕；註冊 census；qm 全套。

**上機驗證（5Q4C，要先問）**：q1_q2、`measure=both`、b 從 −0.498 到 +0.34（2 mV，420 點；b = −0.5 需要
`amplitude_scale` = 2，會被拒絕）、300 次平均、`--no-update`。
預期 q1 的交叉點在線上電壓約 0.27 V（b ≈ +0.11）與 −0.12 V（b ≈ −0.28），q2 的在兩者外側；高頻成員在內圈，
也就是中心是 apex。之後換 q2_q3（idle 0.06 V）。結果的比對方法見 §6.2。

**實作參考點**（2026-09-27 讀過，照這些模式做）

- scqat：`estimators/qubit_ramsey_flux_pulse/`（subpackage 的樣子）、`tools/sweep_order.ascending`、
  `core/figures.render_figures`、`tests/conftest.py` 的 `order_free` fixture、`tools/fit_transmon_freq_flux`
  （arch 的頻率公式）、`estimators/__init__.py`（要加匯出）、`scripts/update_docs.py`。
- SCQO：`_capabilities/flux.py`（mixin、`flux_anchor_v`、敘述常數的寫法）、`_window.py`（只拒絕零寬度）、
  `tests/test_capabilities.py`（`test_flux_pulse_names_carry_the_suffix`、`CAPABILITY_SUMMARIES`）、
  `tests/test_sweep_order.py`；`experiments/pair_zz_coupler.py`（`validate_targets` 檢查 coupler 的 flux
  channel）、`experiments/pair_swap_flux_map.py`（`joint_population` 的 contract、`readout_coords`、
  `_role_names`）；`scripts/update_docs.py`。
- scqo-qm：`experiments/pair_zz_coupler.py`（`select_qubit_pairs`、`vendor_pair_name`、`role_side`、coupler
  以 `amplitude_scale` 播 `const`）、`_pair_roles.JointPopulationMixin`、`_flux_limits.check_flux_pulse_relative` /
  `declared_idle_offset_v`、`_amp_limits.MAX_AMP_SCALE`（= 2）、`experiments/__init__.py` 的 import 行與
  census 測試。

**施工**：一個 feature，落地順序 scqat → SCQO → scqo-qm，最後寫一個 `RELEASES.d` fragment，不切 release。
上機遇到第一個硬體或 gateway 錯誤就停下來回報，動過的 knob 全部還原。

## 4. 實驗 3：`pair_coupler_spectroscopy_swap`（暫名，feature `pair-coupler-spectroscopy-swap`）

> 規格 2026-09-27 寫成並核可，已實作並在 5Q4C q1_q2、q2_q3 上機。已定的四點（使用者，2026-09-27）：ramp
> 兩種形狀都可選（後來改成一個 `ramp_v`）；一次 run 只用一個 LO、跨距 ≤ 500 MHz；每個頻率點穿插不做 ramp 的
> 參考組；SUCCESSFUL 時提議 coupler 的 `f_01_hz`。
> **estimator 修訂 v2（下方「estimator v2」一段）依上機經驗寫成，2026-09-27 核可並實作。**

**問題**：coupler 在目前 idle 下的 0→1 頻率 f_c。

**原理**：在探針（pair 的一個成員）的 xy 上打頻率 f 的 tone。f = f_c 時，tone 透過探針與 coupler 的混成把
coupler 激發起來。接著用 flux ramp 讓 coupler 與探針**慢慢**越過彼此：絕熱通過把 coupler 的激發轉到探針上；
然後**突然**回到 idle（非絕熱，激發留在探針），在 idle 讀取。探針的 P_e 對 f 在 f_c 出峰。參考組完全一樣，
只是不做 ramp，coupler 的激發就留在 coupler 裡衰減，所以參考組只會看到直接激發探針的特徵（探針的躍遷、
讀取、TLS）。**只在 ramp 組出現的峰才是 coupler**。

**5Q4C 的數字**（實驗 1、3 的上機結果與 live config）

- 實測 f01（2026-09-27）：q1_q2_c **7.058 GHz**（idle 0.16 V，α ≈ −135 MHz）、q2_q3_c **7.156 GHz**（idle 0.06 V，
  α ≈ −148 MHz）。實驗 1 的 arch 預測分別是 6.80、7.17 GHz。讀取共振腔在 5.87–6.08 GHz，探針自己的 f01、
  f02/2 都 ≤ 5.2 GHz，都不在視窗內。
- **多光子階梯**：tone 夠強時 coupler 會出現 f02/2 = f01 + α/2、f03/3 ≈ f01 + α 的線。從耦合較強的那條線打
  （q1_q2_c 是 q1 的線、q2_q3_c 是 q3 的線），f01 會被推寬、AC Stark 往上推，最強的反而是 f02/2；從另一條線
  打，f01 才是最強、最穩的線。
- g_qc：由實驗 1 的 dip 寬度估約 50–70 MHz。tone 只能從探針的線打，coupler 的 Rabi 約是
  Ω_q·g/Δ ≈ 3 % 的 Ω_q（Δ ≈ 1.65 GHz）。q1 的 16 ns x180 相當於 −6 dBm、峰值 Rabi 約 62 MHz，所以
  −20 dBm 給 coupler 約 0.4 MHz 的 Rabi，足以飽和（估計誤差約 ±10 dB）。
- ramp（q1_q2，ramp 打在 coupler）：q1 的交叉點在 b = +0.109 V，coupler 在那裡的斜率 22 MHz/mV。0.15 V/µs
  越過交叉點時，g = 50 MHz 的絕熱率 99 %、30 MHz 時 82 %。快邊約 3 ns，漏回 coupler 約 2 %。
- band：q1 的 xy 在 MW-FEM `6/2`、band 1、LO 4.9 GHz，band 1 最多到 5.75 GHz。LO 移到 6.8 GHz 要把 port
  pair（`6/2` + `6/3`）換到 band 2。band 2 的 LO 範圍 4.5–7.5 GHz 也包含 q2 的 4.9 GHz，所以 q2 的 LO
  不用動，只換 band。q3（`6/4`）換 band 會帶著 q4（`6/5`）。

**target**：一個 `qubit_pair`，理由同實驗 1（coupler 線對非鄰居也有 5–7 % 串擾）。`tone_on` 選 tone 走哪個成員的線。

**Parameters**

| 欄位 | 型別 / 預設 | 意義 |
|---|---|---|
| `start_tone_freq_hz` / `end_tone_freq_hz` | 6.55e9 / 7.05e9 | tone 的**絕對**頻率，依 start → end 的順序掃。跨距 ≤ 500 MHz（LO 放在中心、\|IF\| ≤ 250 MHz），超過時依名稱拒絕。以實驗 1 的 `f_c_at_idle_hz` 為中心 |
| `num_tone_freq_points` | 501（v2，原本 251） | 預設 1 MHz 一步：f01 在 −20 dBm 寬 4–7 MHz，2 MHz 一步每個線寬只有 2–3 點。一次 run 約 90 秒。更窄的線（例如 f02/2）另外用窄視窗、細步距量 |
| `tone_power_dbm` | −20 | 探針 xy 的 tone 功率（儀器端 dBm），沿用 `drive_power_boundary`：run 前寫入、run 後精確還原 |
| `tone_len_ns` | 10000 | tone 長度；比 coupler 的 T1 長就是飽和，峰高最多約 0.5 |
| `tone_on`（2026-09-27 由 `probe` 改名，與實驗 2 一致） | `Literal["high","low"]` = `"high"` | 打 tone 的成員。v2 起兩個成員的激發都算（1 − P00），所以它只決定 tone 走哪條線：從耦合較弱的那條線打，f01 最乾淨（5Q4C 兩個 coupler 都是 q2 的線，也就是 q1_q2 的 low、q2_q3 的 low） |
| `ramp_on` | `Literal["coupler","tone_member"]` = `"coupler"` | ramp 打在哪條 flux 線。5Q4C 的 q1 在 apex、coupler 在上方，只能動 coupler；coupler 設計在 qubit 下方時可以改動探針 |
| `ramp_v` | `tuple[float, float] \| None` = None | **播放順序**的（第一點, 最後一點），相對於被 ramp 那條線的 `idle_flux`（使用者 2026-09-27 提議的表述）：輸出先突然跳到第一點，以 `ramp_rate_v_per_us` 線性走到最後一點，再突然回 idle。只有中間的慢段是絕熱的：慢段先越過哪個成員，激發就給誰；兩次跳躍裡經過的交叉點一律非絕熱穿過。`(0, 0.14)` = 先慢後快，swap 在去程；`(0.14, 0)` = 先快後慢，swap 在回程。5Q4C 上只有後者有效：coupler（7.06 GHz）在讀取共振腔（5.87–6.08 GHz）上方、共振腔又在 qubit 上方，回程 swap 時激發先交給 qubit，coupler 之後才經過共振腔。coupler 在兩者上方時，回程先碰到外圈成員（low）。遠端取實驗 1 的交叉點再加約 30 mV（q1_q2：0.14）。None 時依名稱拒絕 |
| `ramp_rate_v_per_us` | 0.15 | 線性段的斜率。Landau–Zener 只看越過交叉點時的斜率，所以兩種形狀在同一個斜率下絕熱性相同，差別只剩 coupler 的 T1 衰減 |
| `flux_buffer_ns` | 100（0，或 ≥16 且為 4 的倍數） | tone 結束到 ramp 開始、以及 ramp 回到 idle 到讀取之間的等待 |
| `num_averages` | 300 | |
| `reset_method` 等 | `QubitResetParameters` | 只能用 thermal：active reset 需要探針的 xy 在 f_q，但這個實驗把 LO 移走了 |

需要 state discrimination（輸出是 `joint_population`），規則同實驗 1。

**序列（每一發）**

1. reset 兩個成員。
2. 探針的 xy 播 `saturation`（`duration` = `tone_len_ns`），頻率由 `update_frequency` 設成 f − LO。
3. 等 `flux_buffer_ns`。
4. ramp 組：被 ramp 的線播一段 arbitrary waveform：第一個取樣就在 `ramp_v` 的第一點，以
   `ramp_rate_v_per_us` 線性走到第二點，結束時輸出回到 idle（突然）。參考組：同樣長度的 `wait`。
5. 等 `flux_buffer_ns` → `align` → 兩個成員都讀取（2-level）→ 存四個 joint indicator。

迴圈順序：averages（外）→ f（依 start → end）→ ramp 組、參考組（內，同一個 f 背靠背，漂移互相抵消）。
q1_q2 下 251 點一次 run 約 50 秒，v2 預設的 501 點約 90 秒。ramp 的長度：`(0.14, 0)` 是 936 ns，
`(0.08, 0.14)` 是 400 ns。

**contract**：sweeps `("tone_freq_hz", "ramp_played")`（`ramp_played` 的座標是 [1, 0]），variables
`("joint_population",)`，readout_dims `("joint_state",)`，`target_kinds = ("qubit_pair",)`。

**estimator v1（已實作，上機後發現的問題）**：只看探針的邊際，取「ramp − 參考」最強的正峰，參考組在同一處
有峰就否決。上機後有三個問題：激發落在哪一顆因 pair 而異（q1_q2 主要在 q1、q2_q3 主要在 q2），只看探針會
漏掉；鄰居的讀取可能直接看到 coupler（q1 看得到 q1_q2_c），參考組有峰不代表不是 coupler；從耦合較強的線打時，
最強的線是 f02/2 而不是 f01（q2_q3 經由 q3 的線時挑中 7.082 而不是 7.156）。

**estimator v2（2026-09-27 核可並實作）**：依使用者 2026-09-27 同意的六點；括號內是實作時定下的細節。

1. **訊號用總激發**：ramp 組 T_r(f) = 1 − P00、參考組 T_0(f) = 1 − P00，差值 D = T_r − T_0。
   另外記下 f01 處兩個成員各自拿到多少（`landing_high`、`landing_low`，只是診斷，不參與判斷）。
2. **找線**：對 T_r 用 `tools.peak_fit.fit_peaks`（merge 關掉，同 v1），每條線都要自己的高度 ≥ `min_snr` × 雜訊，
   **FWHM ≥ `min_fwhm_steps` 個步距（預設 2）**。只有 1–2 個點的訊號一律略掉：那是量測參數的問題，要用更細的
   步距重量，estimator 不去遷就它。（實作：沿用 v1 的 FWHM ≤ 視窗 1/4 與由強到弱去重複；`polarity="peak"`，
   因為 `fit_peaks` 自動判斷正負時，兩條差不多高的線會讓反相訊號裡兩線之間的雜訊點 prominence 跟線一樣大，
   整條被當成 dip、一條線都不剩；每條線的擬合視窗是估計線寬的 2 倍而不是 5 倍，否則 30 MHz 寬的 f01 會
   擬合到 74 MHz 外、更強的 f02/2 上。1 點寬的尖峰從不通過；2 點寬的偶爾剛好過 2 步距，過了也對不上階梯，
   run 判 FAILED，不會被當成 f01。）
3. **哪些線屬於 coupler**：線中心處的 |D| ≥ `min_snr` × D 的雜訊（正負都算），代表 ramp 改變了它、涉及 coupler
   的狀態。|D| 不顯著的線是探針自己的特徵（列在 `member_lines_hz`），不參與判斷。**參考組有線不再自動否決**。
   （實作：「線中心處的 D」是 D 減去中位數後、在中心 ±3 FWHM 內投影到該線自己的單位 Lorentzian 上的高度，
   雜訊是 D 的點對點雜訊除以 √ΣL²，整條線的點都算進去，而不是只看最靠近中心的一點。）
4. **用階梯辨認 f01**：coupler 的線中**最高**的那條當 f01。其餘每一條都要落在它的階梯上：f02/2 = f01 + α/2、
   f03/3 ≈ f01 + α，α 在 −50 到 −400 MHz 之間，容許誤差取 15 MHz 與兩條線 FWHM 較大者的較大值（實測 f03/3
   比 f01 + α 偏 3–11 MHz）。有 f02/2 時由 α = 2(f02/2 − f01) 報 α（附 stderr）；有 f03/3 時一併檢查。
   對不上階梯的 coupler 線設 `unexplained_lines`。（實作：每條其他線各提出兩個 α（當 f02/2、當 f03/3），
   範圍內的都試，放上階梯最多條的勝出；平手時先取有 f02/2 的（只有兩條線時兩種讀法分不開，f02/2 較強、
   較可能），再取放上去的線面積（高 × FWHM）較大的。只有 f01 與一條線、且那條其實是 f03/3 時，會被讀成
   α 兩倍大的 f02/2，這是兩條線本身分不開的情況。）
5. **邊緣**：f01 離視窗邊緣不到一個 FWHM 時設 `peak_at_edge`。
6. **SUCCESSFUL**：有 f01，而且沒有 `unexplained_lines`、`peak_at_edge`。只有一條 coupler 線時就是 f01、不報 α。

**Result（`fit[pair]`，v2）**：`f_c_hz`（f01，附 stderr）、`fwhm_hz`、`peak_height`、`snr`、`alpha_hz`（附 stderr，
沒有 f02/2 時為 NaN）、`n_coupler_lines`、`n_ladder_lines`、`landing_high`、`landing_low`、`lo_hz`、
`ramp_duration_ns`、`old_ramp_idle_flux`，旗標 `no_line`、`unexplained_lines`、`peak_at_edge`。其他線（coupler 的、
探針的）列在 estimator 的 metadata。

**寫回（v2）**：單一 pair、SUCCESSFUL 才提議。coupler mode 的 `f_01_hz`（目前 idle 下的 f01）；有 α 時再加
`anharmonicity_hz`。不動任何 knob。

**simulate（v2）**：用 stable seed 放一組階梯：f01（高 0.35、寬 5 步）與 f02/2（高 0.2、寬 4 步，α 在 −120 到
−160 MHz；3 步寬的線在 300 發下約 5 % 會擬合成不到 2 步而被略掉，所以用 4 步），激發按隨機比例（0.2–0.8）
分給兩個成員；參考組在 high 成員上放 f01 的 20–40 %（模擬鄰居讀取看得到 coupler）；另外在 f01 **上方**放一條
探針自己的線（高 0.3、寬 6 步，兩組相同）：誤判成 coupler 線的話它就會變成 f01。

**QM 的 band / LO**：LO 放在視窗中心。這個 LO 超出 port 目前 band 的範圍時，把整組 port pair 換到涵蓋它的
band；partner 的 LO 若仍在新 band 內就不動，不在的話比照 broadband 停到 band 下限。probe 直接產生**一份
config**（LO、band 已改好），產生完立刻把 QUAM tree 還原，所以不會有 drift；再把綁好這份 config 的
`_lib.acquire` 當成 3-tuple 的 acquire callable 交給 backend。`--preview` 可用，QUA 內容正確，但 preview
用的是 LO 沒移動的 config（它不經過 acquire callable）。

**事前拒絕**：多於一對；coupler 沒有 flux channel；探針缺 drive、readout 或 threshold；`ramp_on=tone_member` 而
探針沒有 flux channel；`ramp_v` 是 None 或兩端相等；跨距超過 500 MHz；零寬度頻率
視窗；rail 與取樣範圍（被 ramp 的線上 idle + `ramp_v` 兩端的最大絕對值，沿用 `_flux_limits`）；active reset。

**只做 QM 版**，理由同實驗 1。

**測試**

- scqat（v2 已改寫）：`tests/test_pair_coupler_spectroscopy_swap_estimator.py`：
  - 兩條線的階梯：f01 取較高的那條、α 正確；
  - f01 被推寬、f02/2 比較強時（5Q4C q2_q3 經由 q3 的線）仍選出 f01；
  - 只有一條線時就是 f01、不報 α；
  - 激發全部落在非探針的成員時照樣找到；
  - 參考組也有 coupler 線時（鄰居讀取看得到）不否決；
  - 兩組相同的探針線被略掉；
  - 對不上階梯的額外 coupler 線報 FAILED；
  - f03/3 放在同一個階梯上；
  - 只有 1–2 個點的尖峰被略掉（2 點寬的偶爾留下時 run 判 FAILED）；
  - `order_free`；
  - 沒有線時報 FAILED，圖仍畫得出來。
  - `tests/test_peak_fit.py`：`polarity` 固定為 peak 時，auto 會判成 dip 的兩條等高線照樣找到。
  - 另外以 40 個種子逐一檢查每個情境（不進測試）。
- SCQO：`test_model_experiments -k spectroscopy_swap`（simulate → estimate 還原放進去的 f01 與 α；提議
  `<coupler>.f_01_hz` 與 `anharmonicity_hz`；多對被拒絕；`ramp_v=None` 被拒絕；跨距 > 500 MHz 被拒絕），
  `test_capabilities` 的 `EXPECTED_CAPABILITIES`（`qubit_reset`；頻率是絕對 Hz，不是 detuning capability），跑
  `update_docs.py`。沒動到 capability mixin，只跑相關的測試檔。
- scqo-qm：用 live quam_state 建 q1_q2 的程式：`update_frequency` 在迴圈內、`saturation` 的長度、ramp
  waveform 的起點／終點／長度、參考組同長度的 `wait`、兩個成員的讀取；產生的 config 裡 q1 的 port 在 band 2、
  LO 在視窗中心，而 QUAM tree 保持原狀；超過 rail 時依名稱拒絕；census；qm 全套。

**上機驗證（5Q4C，要先問）**

1. q1_q2（2026-09-27 已做，細節見 BACKLOG F24）：先慢後快 `(0.08, 0.14)` 找不到 swap 峰；改成先快後慢
   `(0.14, 0)`、probe=low 後成功。**q1_q2_c 在 idle 0.16 V 的 f01 = 7.058 GHz，α ≈ −135 MHz**
   （6.991 = f02/2、6.921 = f03/3；idle 改 0.15 V 時整組上移約 90 MHz）。鄰居的讀取也直接看得到 coupler
   的狀態（參考組，約 5 µs 衰減）。
2. q2_q3（2026-09-27 已做）：`ramp_v=(0.14, 0)`、−20 dBm。**q2_q3_c 在 idle 0.06 V 的 f01 = 7.156 GHz，
   α ≈ −148 MHz**（7.082 = f02/2、7.014 = f03/3）。tone 打在 q2 的線上時 f01 是最強的線；打在 q3 的線上時
   f01 被推寬到約 30 MHz，最強的變成 f02/2（estimator 因此挑錯）。激發主要落在 q2（外圈，符合回程先碰到
   外圈的圖像；q1_q2 卻主要落在內圈的 q1）。參考組是平的：q2、q3 的讀取都看不到 q2_q3_c。arch 預測
   7.17 GHz，只差 14 MHz（q1_q2 差 0.26 GHz）。

3. v2 重新分析這 16 筆（2026-09-27，離線）：先快後慢的 q1_q2 三筆經由 q2 的線都是 SUCCESSFUL，f01 =
   7.0577–7.0589 GHz；q2_q3 經由 q3 的線（v1 挑錯的那筆）現在選到 7.1558 GHz（FWHM 27 MHz）；經由 q2 的線
   7.1556–7.1560，視窗從 7.15 開始的那筆判 `peak_at_edge`；q1_q2 idle 0.15 V 經由 q2 的那筆 f01 貼在視窗邊緣、
   只剩 1–2 點，判 `no_line`。2 MHz 步距下 f02/2 都只有 1–2 點寬，所以都
   沒有報 α：要 α 就用 v2 預設的 1 MHz 步距，或在 f02/2 附近用窄視窗重量。經由 q1 的線時 f01 被 AC Stark
   推高、推寬（7.071–7.077，FWHM 13–34 MHz），α 報成 −161 到 −172 MHz：量測條件的問題，從耦合較弱的線打。
   **一筆誤判**：183300（先慢後快、經由 q1 的線、−10 dBm）ramp 組裡 f01 根本不成線（參考組有一大片被推寬的
   coupler 訊號），只剩 f03/3 的 6.921，於是被當成 f01 判 SUCCESSFUL。這是已知不可用的量測設定；v2 的規則
   在 f01 缺席時分辨不出來。
4. v2 上機（2026-09-27，run 20260927-203446-486）：q1_q2、6.85–7.35 GHz、501 點（1 MHz）、從 q2 的線打、
   `ramp_v=(0.14, 0)`、−20 dBm。**SUCCESSFUL，f01 = 7.0575 GHz ± 0.35 MHz**（FWHM 8.7 MHz、SNR 28），只有
   一條 coupler 線，所以沒有 α：從 q2 的線打、−20 dBm 時 f02/2（6.991）在 1 MHz 步距下看不到，只有單點
   尖峰，照規則略掉。激發 q1 拿到 0.14、q2 拿到 0.10。提議的 `f_01_hz` 留在 run 上待決定（沒有用
   `--no-update`），device 沒動；q2_xy 的 `drive_power_dbm` run 前後都是 0 dBm，已還原。要 α 得讓 f02/2
   變寬：從 q2 的線加功率，或從 q1 的線打（但 f01 會被 AC Stark 推高）。α 目前不是重點（使用者，2026-09-27），
   不追。

找不到峰時先移視窗（±250 MHz），再調功率（±10 dB）。辨認 f01 看多光子階梯（f02/2 = f01 + α/2、
f03/3 ≈ f01 + α），並從耦合較弱的那條線打 tone；不要只看哪條線在低功率還在。

**實作參考點**：scqat `tools/peak_fit.fit_peaks`、實驗 1 的 estimator（subpackage、`render_figures`、
`order_free`）；SCQO `pair_coupler_crossing_pulse.py`（validate_targets、`_role_names`、joint readout）、
`broadband_qubit_spectroscopy.py`（絕對 Hz 視窗）、`_drive_power.drive_power_boundary`；scqo-qm
`pair_coupler_crossing_pulse.py`、`broadband_qubit_spectroscopy.py`（`_MW_FEM_BANDS`、`_partner_port_id`）、
`qm_backend.acquire` 的 3-tuple acquire callable 路徑、`_lib.acquire(config=)`。

## 5. 實驗 2：`pair_coupler_spectroscopy_zz`（暫名，feature `pair-coupler-spectroscopy-zz`）

> 規格 2026-09-27 寫成並核可。π 先用校正好的 x180（使用者：「先用 x gate 參數試試看，沒訊號再改造 pulse
> shape」），上機沒有訊號，同日改成**選擇性 π**，並且 **dip 只從 π 組找**（使用者核可；參考組改為診斷）。
> α 不是這個實驗的重點（使用者，2026-09-27）：階梯只用來認出 f01，順便有 f02/2 時才報 α。

**問題**：coupler 在目前 idle 下的 0→1 頻率 f_c，coupler 的 flux 完全不動。

**原理**：tone 經由一個成員（**tone 成員**）的 xy 線激發 coupler，同實驗 3。tone 結束後，另一個成員（**π 成員**）
打一個 π。π 的頻寬比 coupler 與 π 成員之間的 ZZ 窄時（**選擇性 π**），coupler 在基態時 π 打得上去；
coupler 被激發時，ZZ 把 π 成員的頻率推開，π 打偏，P_e 下降，所以 π 組在 coupler 的線上出現 dip。參考組
同一發不打 π，只當診斷（見 estimator 第 1 步）。跟實驗 3 互補：不需要 ramp、不需要越過交叉點，也不依賴讀取共振腔
的位置。

**5Q4C 的數字**

- **ZZ**（兩個 Duffing 振子數值對角化）：q1 對 q1_q2_c（7.058 GHz、α −135 MHz）在 g = 40 / 50 / 60 / 70 MHz 時
  是 −0.30 / −0.46 / −0.66 / −0.90 MHz；q3 對 q2_q3_c（7.156 GHz、α −148 MHz）幾乎一樣（−0.29 到 −0.89）；q2 對
  q1_q2_c 小一些（−0.23 到 −0.70）。原本這裡估的 2–8 MHz 是用 Δ ≈ 1 GHz 算的，實測 Δ ≈ 1.9 GHz。
- **x180 看不到**（16 ns DragCosine，頻寬約 ±30 MHz）：ZZ 0.3–0.9 MHz 只讓 P_e 掉約 (ζ/Ω)² < 0.1 %。上機確認
  （見「上機結果」1）。
- **選擇性 π 的包絡與長度**：coupler 在 π 期間會衰減（T1 約 5 µs），所以脈衝不能太長；包絡越平滑，同樣的選擇性
  要越長。數值模擬（二能階 qubit，coupler 在脈衝中途隨機衰減，衰減後 qubit 不再被推開）每單位 coupler 佔據
  留下的 dip，ZZ 取 0.3 / 0.45 / 0.66 / 0.9 MHz：

  | 包絡 | 1 µs | 2 µs | 3 µs | 4 µs | 6 µs |
  |---|---|---|---|---|---|
  | 方波 | 0.28–0.91 | **0.67–0.83** | 0.70–0.76 | 0.66–0.69 | 0.58 |
  | cos（Hann） | 0.11–0.66 | 0.34–0.83 | 0.54–0.75 | 0.62–0.69 | 0.57 |
  | Gauss（σ = L/6） | 0.09–0.57 | 0.28–0.81 | 0.46–0.75 | 0.56–0.69 | 0.56–0.57 |

  平滑包絡沒有旁瓣，但主瓣約寬一倍，要到 4 µs 才跟 2 µs 的方波相當，而那時 coupler 已衰減掉更多；6 µs 以上
  三者都被衰減卡在約 0.57。所以用**方波、預設 2 µs**（ZZ 小於 0.4 MHz 時 3 µs 較平）。q1 的 T2* 33 µs 在 2 µs
  內損失約 6 %，q3 的 T2* 9.4 µs 約 19 %。
- **選擇性 π 的振幅**：保留 π 成員校正好的 x180 的**旋轉面積**。x180 帶有自己的 frame detuning（q1：−8.7 MHz，
  補償強脈衝的 Stark 位移，又弱又長的脈衝沒有這個位移），要先把它轉掉再加總：16 ns 的 DragCosine、振幅 A
  正好是 7.5 A ns（q1：1.606 ns）。直接加總會少 12 %。方波振幅 = 面積 / L，q1 在 2 µs 時 8.0 × 10⁻⁴，也就是
  `saturation` 振幅的 0.18 %。`x180_Square` 從沒校正過（振幅還是預設的 0.1），不能拿來拉長。
- **訊號大小**：實驗 3 經由 q2 的線、−20 dBm 時 swap 後的總激發約 0.35；coupler 的 T1 約 5 µs（實驗 3 的延遲
  測試），2 µs 的 π 期間約掉 20–30 %。預期 dip 深約 0.35 × 0.75 × 0.9 ≈ 0.25，300 發的雜訊約 0.03。
- **port**：tone 走 q2 的線（實驗 3 的經驗：耦合較弱的線，f01 最乾淨），π 打在另一個成員（q1_q2 是 q1、q2_q3 是
  q3，都是耦合較強、ZZ 較大的那顆）。q2 的 port（`6/3`）LO 移到視窗中心、換到 band 2；同一組 port pair 的 q1
  （`6/2`）跟著換 band 2，LO 4.9 GHz 不動（band 2 涵蓋 4.5–7.5 GHz），IF 仍是 +244.6 MHz。q3 在 `6/4`（另一組
  port pair），完全不動。**q1_q2 的 π 是在換過 band 的 port 上打的**：如果 MW-FEM 在 band 2 的輸出跟 band 1 不同，
  π 的面積會偏，estimator 報的 `pi_contrast` 看得出來。
- **一次 run**：501 點 × 2 組 × 300 發，每發約 0.31 ms（q1 的 thermalization 0.3 ms 主導），約 95 秒，同實驗 3。

**target**：一個 `qubit_pair`，理由同實驗 1、3。

**Parameters**

| 欄位 | 型別 / 預設 | 意義 |
|---|---|---|
| `start_tone_freq_hz` / `end_tone_freq_hz` | 6.55e9 / 7.05e9 | 同實驗 3：絕對 Hz、依 start → end 的順序掃、跨距 ≤ 500 MHz |
| `num_tone_freq_points` | 501 | 同實驗 3，預設 1 MHz 一步 |
| `tone_power_dbm` | −20 | 同實驗 3，只套在 tone 成員的 drive channel，run 後精確還原 |
| `tone_len_ns` | 10000 | 同實驗 3 |
| `selective_pi_len_ns` | 2000（≥ 16、4 的倍數） | 選擇性 π 的長度；振幅由 π 成員 x180 的旋轉面積換算，不另外校正 |
| `tone_on` | `Literal["high","low"]` = `"low"` | tone 走哪個成員的線；選擇性 π 打在**另一個**成員。5Q4C 兩個 coupler 都是 low（q2 的線） |
| `num_averages` | 300 | |
| `reset_method` 等 | `QubitResetParameters` | 只能 thermal，理由同實驗 3（tone 成員的 LO 被移走） |

沒有任何 flux 欄位：coupler 停在 idle，`old_coupler_idle_flux` 只記錄來源。需要 state discrimination。

**序列（每一發）**

1. reset 兩個成員。
2. tone 成員的 xy 播 `saturation`（`duration` = `tone_len_ns`），頻率由 `update_frequency` 設成 f − LO。
3. `align` → π 組：π 成員的 xy 在自己的 `drive_freq_hz` 播選擇性 π（`saturation`、`amplitude_scale` 換算自
   x180 的面積、`duration` = `selective_pi_len_ns`）；參考組：同樣長度的 `wait`。
4. `align` → 兩個成員都讀取（2-level）→ 存四個 joint indicator。

迴圈順序：averages（外）→ f（依 start → end）→ π 組、參考組（內，同一個 f 背靠背）。tone 與 π 不重疊：π 期間
tone 已關，π 成員不會被 tone 的 AC Stark 推開；代價是 coupler 在 π 期間衰減（已算進上面的表）。

**contract**：sweeps `("tone_freq_hz", "pi_played")`（`pi_played` 的座標是 [1, 0]），variables
`("joint_population",)`，readout_dims `("joint_state",)`，`target_kinds = ("qubit_pair",)`。

**estimator（scqat，新的，1:1）：`pair_coupler_spectroscopy_zz`**

1. π 成員的邊際：π 組 P_π(f)、參考組 P_0(f)。**只用 π 組找 dip，兩組不相減**：π 成員的讀取直接看到 coupler 的
   效應跟它的狀態有關（5Q4C q1 在 |0⟩ 時看得到 q1_q2_c 的 f01、f02/2，在 |1⟩ 時看不到），相減會在 coupler 的線上
   挖出跟 ZZ 無關的假 dip。參考組自己的峰列成 `readout_lines_hz`，只當診斷。`pi_contrast` = P_π 的中位數 −
   P_0 的中位數（π 的效率乘上讀取對比，正常約 0.85）。
2. **找線**：P_π 的 **dip**，規則同實驗 3 v2 的第 2 步（高度 ≥ `min_snr` × 雜訊、FWHM ≥ `min_fwhm_steps` 個步距
   （預設 2）且 ≤ 視窗 1/4、merge 關、擬合視窗是線寬的 2 倍）。（實作：dip 是在 −P_π 上找峰。`fit_peaks` 自己的
   dip 路徑把擬合起點放在反相後的最低點，也就是肩上的雜訊：一條乾淨、深 0.25 的 dip，40 次有 24 次擬合錯、
   3 次找不到；另開任務修。）
3. **每一條 dip 都算 coupler 的**：tone 在 π 之前就關了，能讓 π 打偏的只有 tone 留下來的激發（coupler，偶爾一個
   TLS），所以沒有實驗 3 第 3 步那種「ramp 有沒有改變它」的判斷。
4. **f01 = 最高的那條**，其餘要落在它的階梯上（同實驗 3 v2 第 4 步）；對不上的設 `unexplained_lines`。
5. `peak_at_edge` 同實驗 3。
6. **SUCCESSFUL**：有 f01，而且沒有 `unexplained_lines`、`peak_at_edge`。`pi_contrast` 只報告不判斷：π 打得不準時
   dip 變淺，位置不變。

**共用程式**：實驗 3 estimator 裡的找線與階梯搬到 `scqat/tools/coupler_ladder.py`（`find_lines`、`lines_curve`、
`read_ladder`，純數學，兩個 estimator 共用；實驗 3 的行為不變，它的測試與 16 筆實測的判讀都照舊）。SCQO 的 tone 視窗欄位與驗證（跨距、零寬度、
`lo_hz`）搬到 `scqo/experiments/_coupler_tone.py` 的 mixin（不是 capability）；scqo-qm 的移 LO、換 band、還原
QUAM tree 搬到 `scqo_qm/experiments/_coupler_tone.py`。三處都只是搬家，實驗 3 不改行為。

**Result（`fit[pair]`）**：`f_c_hz`（附 stderr）、`fwhm_hz`、`dip_depth`、`snr`、`alpha_hz`（附 stderr，沒有 f02/2 時
為 NaN）、`n_lines`、`n_ladder_lines`、`pi_contrast`、`lo_hz`、
`old_coupler_idle_flux`，旗標 `no_line`、`unexplained_lines`、`peak_at_edge`。線的清單（含參考組自己的
`readout_lines_hz`）在 estimator 的 metadata。

**寫回**：單一 pair、SUCCESSFUL 才提議：coupler mode 的 `f_01_hz`，有 α 時再加 `anharmonicity_hz`（同實驗 3）。
不動任何 knob。實驗 2 與 3 各自保留寫回，由操作程序依情況選。

**simulate**：用 stable seed 放一組階梯（f01 佔據 0.45、寬 5 步；f02/2 佔據 0.3、寬 4 步），當成 coupler 的佔據
p_c(f)（比實驗 3 的 0.35 / 0.2 高，因為 π 成員停在 0.9 附近，D 的雜訊約 0.02，比實驗 3 大；這代表的是選擇性
π 會看到的 dip）。π 成員的讀取在它處於 |0⟩ 時看得到 coupler 佔據的 20–40 %（share）：參考組 P = 0.02 + share ×
p_c；π 組 P = 0.02 + 0.88 × (1 − 0.9 p_c) + share × 0.9 p_c（被打偏、留在 |0⟩ 的那部分）。tone 成員兩組都是
0.02。

**QM**

- LO 與 band：同實驗 3（tone 成員的 port LO 放在視窗中心，超出 band 時整組 port pair 換 band；partner 的 LO 在新
  band 內就不動；產生 config 後立刻還原 QUAM tree）。π 成員若是 tone 成員的 port-pair partner（q1_q2 的 q1），
  只跟著換 band。
- 選擇性 π：取 x180 的 `calculate_waveform()`，乘上 exp(−i2π·detuning·t) 轉掉它自己的 frame detuning 後加總取
  大小 × 1 ns = 面積；方波振幅 = 面積 / L，以 `saturation` 播放、`amplitude_scale` = 方波振幅 / saturation 振幅、
  `duration = L / 4` 個 clock。config 裡不加新的 waveform。

**事前拒絕**：多於一對；pair 沒有 coupler，或 coupler 沒有 flux channel（同實驗 1、3 的 gate，
`old_coupler_idle_flux` 從它讀）；成員缺 drive、readout 或 threshold；跨距超過 500 MHz；零寬度視窗；
active reset；`selective_pi_len_ns` 不在 4 ns 格點上或短於 16 ns；driver 端：π 成員缺 `x180` 或 `saturation`、
面積不是正的有限值、換算出的振幅不小於 saturation 振幅（脈衝要更長）。

**只做 QM 版**，理由同實驗 1。

**測試**

- scqat：`tests/test_pair_coupler_spectroscopy_zz_estimator.py`：
  - dip 的 f01 還原；
  - 兩條線的階梯（f01 取較高的那條、有 f02/2 時報 α）；
  - 只有一條線時就是 f01、不報 α；
  - π 成員的讀取看得到 coupler（只在 |0⟩）時，f01 照樣對，參考組的峰列在 `readout_lines_hz`；
  - π 打不偏（x180 的情況）而參考組有峰時報 `no_line`，不會被相減挖出假 dip；
  - 對不上階梯的額外 dip 報 FAILED；
  - 只有 1–2 個點的尖峰被略掉；
  - π 打得不準（`pi_contrast` 約 0.4）時 f01 照樣對；
  - 沒有 dip 時報 `no_line`、`pi_contrast` 照報；
  - `order_free`；
  - 沒有線時報 FAILED，圖仍畫得出來。
  - `tests/test_coupler_ladder.py`（tool）；實驗 3 的測試照舊要過。
- SCQO：`test_model_experiments -k coupler`（simulate → estimate 還原 f01；提議 `f_01_hz`；多對被拒絕；跨距
  > 500 MHz 被拒絕；實驗 3 照舊），`test_capabilities` 的 `EXPECTED_CAPABILITIES`（`qubit_reset`），跑
  `update_docs.py`。
- scqo-qm：面積轉掉 frame detuning（純函式）；方波保留 x180 的面積；缺 operation 時依名稱拒絕；用 live
  quam_state 建 q1_q2 的程式：tone 在 q2 的 xy 上 `update_frequency`、`saturation` 的長度；q1 的選擇性 π
  （`saturation*amp(...)`、`duration=500`，程式裡沒有 x180）；參考組同長度的 `wait`；兩個成員的讀取；config 裡 q2 的 port 在 band 2、LO 在視窗中心，
  q1 同一 band、LO 4.9 GHz，QUAM tree 保持原狀；q2_q3 時 q3 的 port 不動；census；qm 全套。

**上機驗證（5Q4C，要先問）**

1. q1_q2：6.85–7.35 GHz、501 點、`tone_on=low`（q2 的線）、−20 dBm、x180 打在 q1。預期沒有 dip（已做，見下）。
2. 選擇性 π（2 µs 方波），同樣設定再跑 q1_q2：預期 dip 在 7.0575 GHz（實驗 3 同日的值），深約 0.35 × 0.7 ×
   (0.88 − share) ≈ 0.15–0.2。然後 q2_q3（π 打在 q3，預期 7.156 GHz）。

f01 要跟實驗 3 在一個 FWHM 內一致（§6.2）。選擇性 π 仍找不到 dip 時：先看 `pi_contrast`（π 本身打不上），再把
π 拉長到 3–4 µs（ZZ 比預期小）。

**上機結果**

1. x180（2026-09-27，run 20260927-211451-900，q1_q2、6.85–7.35 GHz、501 點、tone 在 q2 的線、−20 dBm、x180 打在
   q1）：**如預期沒有 ZZ 的 dip**，判 `no_line`。π 組平在約 0.92，`pi_contrast` 0.89：q1 在換到 band 2 的 port 上
   x180 照樣打得準。device 沒動（FAILED 沒有提議），q2_xy 的 `drive_power_dbm` 還原為 0 dBm。
   **發現**：參考組（q1 在 |0⟩）在 7.057 與 6.991 GHz（coupler 的 f01、f02/2）有 0.09–0.12 的窄峰，是 q1 的讀取直接
   看到 coupler；π 組（q1 在 |1⟩）看不到。這個效應跟 q1 的狀態有關，所以「π 組 − 參考組」**不會抵消**它，反而在
   coupler 的線上造成跟 ZZ 無關的假 dip。這次它們只有 1–2 點寬，被寬度門檻略掉；換成選擇性 π 前要先決定
   參考組怎麼用（見 BACKLOG F24）。
2. 選擇性 π（2026-09-27，run 20260927-213623-198，同樣設定、2 µs 方波打在 q1、dip 只從 π 組找）：**SUCCESSFUL，
   q1_q2_c 的 f01 = 7.05695 GHz ± 0.13 MHz**（FWHM 6.9 MHz、dip 深 0.40、SNR 18），跟實驗 3 同日的 7.0575 GHz
   差 0.5 MHz、在一個 FWHM 內（§6.2）。dip 比預估的 0.15–0.2 深一倍多：coupler 的佔據或 ZZ 比估計大。f02/2
   （6.991 GHz）只有一個點的 dip，照規則略掉，所以沒有 α。`pi_contrast` 0.80（x180 時 0.89：2 µs 的 π 在 T2*
   下的損失加上面積換算的誤差）。參考組在 f01 只剩約 0.05 的小峰，沒有列進 `readout_lines_hz`。q2_xy 的
   `drive_power_dbm` 還原為 0 dBm。使用者認為兩個實驗夠接近、任選其一：**`q1_q2_c.f_01_hz` = 7.05695 GHz 已寫入**
   （accept 這個 run 的提議）。
3. q2_q3（2026-09-27，run 20260927-214341-704，6.9–7.4 GHz、501 點、tone 在 q2 的線、−20 dBm、2 µs 選擇性 π 打在
   q3）：**SUCCESSFUL，q2_q3_c 的 f01 = 7.15550 GHz ± 0.23 MHz**（FWHM 8.2 MHz、dip 深 0.31、SNR 14），實驗 3 是
   7.1555–7.1560 GHz。f02/2（7.082）又是一個點的 dip，沒有 α。`pi_contrast` 0.78（q3 的 T2* 較短）。參考組是平的：
   q3 的讀取看不到 q2_q3_c（跟實驗 3 一致）。**`q2_q3_c.f_01_hz` = 7.15550 GHz 已寫入**（使用者）；q2_xy 的
   `drive_power_dbm` 還原為 0 dBm。

**已定（使用者 2026-09-27）**

1. tone 與 π 分在兩個成員：tone 走耦合較弱的線，π 打在耦合較強、ZZ 較大的那顆。這樣不需要在同一個 port 開
   第二個 upconverter。
2. π 先用校正好的 x180；沒訊號再改成選擇性 π（方波、預設 2 µs、振幅由 x180 的面積換算）。x180 上機沒有訊號，
   已改成選擇性 π（使用者同意；包絡的比較見上方的表）。
3. 參考組不打 π（同長度的 wait）。**dip 只從 π 組找**（使用者，x180 上機之後）：參考組自己的峰只當診斷。
4. tone 結束後才打 π，兩者不重疊。

**實作參考點**：實驗 3 的三個檔（SCQO `pair_coupler_spectroscopy_swap.py`、scqat 同名 estimator、scqo-qm 同名
probe）是最直接的範本；`_drive_power.drive_power_boundary(targets=)`；scqo-qm `_pair_roles.JointPopulationMixin`、
QUAM pulse 的 `calculate_waveform()`。

**施工**：一個 feature，落地順序 scqat → SCQO → scqo-qm，最後寫一個 `RELEASES.d` fragment，不切 release。
上機遇到第一個硬體或 gateway 錯誤就停下來回報，動過的 knob 全部還原。

## 6. 驗證方法與參考數據

### 6.1 透過鄰居讀 coupler（2026-09-26 的 DC 量測；優先度低，只拿來驗證）

**鄰居頻率 vs coupler DC**（`coupler_scan.py`）：`q1_q2_c_z.idle_flux` 以 20 mV 為一步 ping-pong 掃描，每點量兩次
Ramsey（6 MHz detuning / 4 µs / 201 點 / periodogram）。q1+q3 一起量，q2 單獨量（q1 和 q2 同時處於疊加態會
帶進 ZZ）。q3 不是這個 coupler 的鄰居，當作對照組。數值是相對各自 drive 的偏差（kHz）：

| coupler (V) | q1（鄰居，high） | q2（鄰居，low） | q3（對照組） |
|---|---|---|---|
| 0.020 | −494 | −4645 | −1663 |
| 0.040 | −153 | −3269 | −1193 |
| 0.060 | +93 | −2136 | −804 |
| 0.080 | +260 | −1227 | −490 |
| 0.100 | +347 | −558 | −254 |
| 0.120 | +344 | −116 | −93 |
| 0.140 | +242 | +82 | −6 |
| **0.160**（idle） | +19 / +19 | +29 / +28 | +3 / +6 |
| 0.180 | −381 | −321 | −56 |
| 0.200 | −1083 | −1013 | −201 |
| 0.220 | −2393 | −2201（c=0.44） | −424 |
| 0.240 | −5508（c=0.23） | −4320（c=0.10） | −724 |

中心點前後各量一次（相隔約 12 分鐘），差距 ≤ 3 kHz。0.24 V 時 q1/q2 的對比度塌掉：qubit 被推開好幾 MHz，
x90 就不再共振。

**兩個 coupler 偏壓下的 qubit DC apex**（`apex_vs_coupler.py`）：每個偏壓 5 點加 1 個重複點，q1 與 q3 一起量。
m 的定義：coupler 線每 1 V 等效於 qubit 自己的線 +m V，所以 apex 位置移動 −m·ΔV_c。

| coupler 0.16 → 0.08 V | apex **位置**移動 | apex **高度**移動（kHz） |
|---|---|---|
| q1（鄰居） | +4.36 mV → m₁ = +5.45 % | **+524**（coupler 的 Lamb shift） |
| q3（對照組） | +5.73 mV → m₃ = +7.16 % | +119 |

**讀法**

- 鄰居的原始曲線是兩樣東西的和：串擾移動 apex 的**位置**，coupler 的 Lamb shift 移動 apex 的**高度**。
  原始曲線的頂點（q1 在 101 mV、q2 在 143 mV）兩者都不是。q3 的曲線則是純串擾（220 mV 內都是拋物線，
  |m| ≈ 7.4 %，與上表一致）。
- 用 m₁ 扣掉串擾，還原出 q1 的純 Lamb 曲線：頂點約 74 mV，所以 **q1_q2_c 的 DC apex 約在 0.074 V**
  （誤差估計 ±2 mV）。idle 0.16 V 在它上方 86 mV，位在陡的那一側。

### 6.2 怎麼驗證三個實驗的結果

- **實驗 1 的中心**跟 §6.1 還原的 DC apex 0.074 V（±2 mV）比。兩者的差距來自 coupler 線的 pulse/DC 比例 g_c。
- **g_c**：同一小時內，用 DC 移動 coupler 量一次 apex（§6.1 的鄰居 apex 掃描，或把實驗 1 的序列改成 DC 版），
  再跟實驗 1 的 pulse frame 結果比。qubit 線的 g ≈ 0.96 就是用同樣方式量的（在 `qubit_ramsey_flux_pulse` 上，
  拿同一小時的 DC 參考比）。
- **實驗 1 的交叉點**跟 §2 的外推比：q1 約在線上電壓 0.27 V 與 −0.12 V，q2 在兩者外側。
- **實驗 3 與實驗 2**在同一個 coupler 偏壓下量到的 f_c 要一致，而且要落在實驗 1 的 arch 模型在 idle 處預測的
  f_c 範圍內（在實驗 1 的精度以內）。
- 在兩個 coupler 偏壓下各跑一次實驗 3，f_c 的變化要符合實驗 1 的 arch。

## 7. 類比用的既有實驗

- **`qubit_spectroscopy`**：1D 驅動頻率掃描加上 saturation drive。實驗 2、3 的 coupler tone 就是它的 coupler 版。
- **`broadband_qubit_spectroscopy`**：用絕對的 [start, stop] 頻率視窗，分段換 LO，換 band 時會拖著 port pair 的
  另一顆，並在 `finally` 還原。實驗 3 的頻率掃描沿用這個做法。
- **`qubit_spectroscopy_flux_pulse`、`qubit_ramsey_flux_pulse`**：pulse frame（脈衝疊在 idle 上）、名稱以
  `_pulse` 結尾、寫回時重新參照成絕對值（`old_idle + 擬合值`）。實驗 1 的 coupler 視窗照這個模式。
- **`pair_zz_coupler`**：pair 為 target、用 `measure` 選成員、coupler 以 `const` 脈衝疊在 `decouple_offset` 上
  （`check_flux_pulse_relative` 做 rail 檢查），也是只做 QM 版的前例。它的寫回 frame 有問題（I26），這裡只借它
  的 target 與 probe 形狀。
- **`pair_swap_flux_map`、`pair_swap_chevron`**：`coupler_flux_v` 軸的意思（疊在 coupler idle 上的脈衝振幅），
  以及透過 `JointPopulationMixin` 讀成 `joint_population`。
