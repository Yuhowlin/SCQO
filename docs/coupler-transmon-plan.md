# coupler 當 transmon：借用 drive channel 與映射讀出（第一個實驗：qubit_power_rabi）

> 狀態：規格已核可（2026-09-28，使用者：「照你原訂規畫，不要折衷版」；§10 六項都採建議，決定 5 選 (b)）。
> 同日實作完成、離線驗證通過，在四個 repo 的 `feature/coupler-transmon`：scqat cea9ea8、SCQO e0e3312、
> scqo-qm f0adf0c、scqo-qblox a7a57a2。實作跟原稿不同的地方已寫進本文（§2.2 相容性、§2.5 view 的作法、
> §2.6、§5）。§8 步驟 0–7 已於 2026-09-28 在 5Q4C 全部完成：`xy2.q1_q2_c.pi_amp` = 0.0702（經 q1 讀出，
> 閉合倍率 0.995）、`xy2.q2_q3_c.pi_amp` = 0.0597（經 q3 讀出，閉合倍率 0.991）；途中 q1 因埠 6/2 換到頻段 2
> 而重新校正。結果與經過見 BACKLOG F24。
> 範圍：只把一個實驗設計到能實作的細節——`qubit_power_rabi` 打在 5Q4C 的 `q1_q2_c`，經 q2 的線 `xy2`
> 驅動、經 q1 讀出。其餘 `qubit_*` 實驗之後一個一個接，記在 BACKLOG F24。
> 依據：`docs/coupler-readout-plan.md` §5（選擇性 π 與它在 5Q4C 上的結果）、`docs/store-by-line-plan.md`
> §2.2（借用 channel）。硬體數字取自 `D:\qpu_data_dev\5Q4C\cd2\qm_5q`（state.json、scqo_state.json、
> physical.json，2026-09-28）。

## 0. 已定（使用者 2026-09-28）

1. **擴充既有的 `qubit_*` 實驗，不另開 `coupler_*`。**
   - roster 本來就把 coupler 宣告成 `flux_transmon`（5Q4C 的 `components.toml`），`target_kinds` 會放行。
   - 擋住它的只有 `required_operations = ("rx", "readout")`：這兩項是從設計接線推出來的，而 coupler 只接了
     flux 線 `zc12`。
   - CLAUDE.md〈The estimator binding〉規則 4：軸、資料形狀、模型、寫回欄位都相同，就是同一個實驗，用
     Parameters 欄位切換。另開一個實驗，1:1 規則會要一個跟 `power_rabi` 一模一樣的 estimator。
2. **兩個彼此獨立的 Parameters mixin**：drive 走哪條線（`drive_line`）、讀出經過誰（`readout_member`）。預設值
   就是今天的行為，打在 qubit 上的序列一個 pulse 都不變。
3. **第一個採用的實驗是 `qubit_power_rabi`**，estimator 的模型不變。
4. **QM 實作，Qblox 按名稱拒絕**（backend parity 的例外條款：做不到的選用能力要按名稱拒絕）。

## 1. 實驗：q1_q2_c 的 power Rabi

### 1.1 每一發

| 步驟 | element | 內容 |
|---|---|---|
| 1 | q1 | reset：thermal，等 q1 的 thermalization（301.2 µs） |
| 2 | 全部 | `align` |
| 3 | `xy2.q1_q2_c` | `x180` × a：200 ns cosine，7.05695 GHz；a 是掃描的倍率 |
| 4 | 全部 | `align` |
| 5 | q1.xy | 選擇性 π：`saturation` × 0.00180，2000 ns，在 q1 平常的 drive 頻率 |
| 6 | q1.xy | `x180`（現有校正，16 ns） |
| 7 | q1 | 讀取（2-level，discriminated） |

- 步驟 5–7 就是映射讀出（§1.2）。
- 迴圈同現在的 `qubit_power_rabi`：averages（外）→ a（依 start → end）。
- 一發約 0.305 ms，由 reset 主導；101 點 × 300 發約 9 秒。

### 1.2 映射讀出：把 coupler 的狀態複製到 q1

選擇性 π 的頻寬約 0.5 MHz，比 q1 與 q1_q2_c 之間的 ZZ（數值估計 −0.3 到 −0.9 MHz）窄，所以只在 coupler 位於
|0⟩ 時打得上。後面再補一個 q1 的 x180：

| coupler | 選擇性 π 之後的 q1 | x180 之後的 q1 |
|---|---|---|
| \|0⟩ | 1 | 0 |
| \|1⟩（ZZ 把 q1 推開，π 打偏） | 0 | 1 |

所以 q1 的 P_e 跟 coupler 的激發同向。補 x180 有兩個理由：

- **極性正常。** 之後接上的實驗（T1、Ramsey…）不必逐一檢查 estimator 對反相訊號的假設。
- **消掉讀取的假訊號。** q1 在 |0⟩ 時，它的讀取直接看得到 coupler：zz 實驗的參考組在 f01 有 0.09–0.12 的凸起
  （run 20260927-211451-900）。補了 x180 之後，coupler 激發時 q1 在 |1⟩，這個效應不會出現；不補的話，它會把
  dip 填回去一部分。

代價是多一個 16 ns 的閘（q1 x180 的誤差約 1%）。

選擇性 π 的構造完全沿用 zz 實驗（coupler-readout-plan §5）：
- 面積：取 q1 x180 的 `calculate_waveform()`，先轉掉它自己的 frame detuning（−8.7 MHz）再加總，得 1.606 ns。
- 方波振幅 = 面積 / 2000 ns = 8.0 × 10⁻⁴，以 `saturation`（振幅 0.44668）× 0.00180 播放，`duration` 500 個
  clock。
- 不需要新的校正。

### 1.3 5Q4C 的數字

| 項目 | 值 | 來源 |
|---|---|---|
| coupler f01（idle 0.16 V） | 7.05695 GHz ± 0.13 MHz | physical.json `q1_q2_c.f_01_hz`；run 20260927-213623-198 |
| coupler α | 約 −135 MHz | 實驗 3 的階梯（f02/2 = 6.991 GHz），沒有寫進 store |
| coupler T1 | 約 5 µs | 實驗 3 的延遲測試（run 20260927-182612-752） |
| `xy2` 的 port | `con1/6/3`，band 1，LO 4.9 GHz，full scale +7 dBm | state.json |
| q2（同一個 port） | 4.84215 GHz，IF −57.85 MHz，x180 振幅 0.21421 | state.json |
| q1（讀出 member） | 5.14460 GHz，`con1/6/2`（同一組 port pair），LO 4.9 GHz，IF +244.60 MHz | state.json |
| q1 的 x180 | 16 ns DragCosine，振幅 0.21411，frame detuning −8.7 MHz | state.json |
| q1 的 saturation | 20 µs 方波，振幅 0.44668 | state.json |
| q1 的讀取 | 800 ns，depletion 400 ns，fidelity g/e 0.971 / 0.934，threshold 已校正 | scqo_state.json `feedline.q1` |
| q1 的 thermalization | 301.2 µs | scqo_state.json `xy1.q1` |
| 選擇性 π 的對比 c₀ | 0.80 | run 20260927-213623-198 的 `pi_contrast` |
| coupler 激發時 π 打偏的比例 s | 模擬 0.67–0.83（含 2 µs 內的衰減）；飽和時 dip 0.40 ≈ 對比的一半，實際可能接近 1 | coupler-readout-plan §5 |

### 1.4 π 振幅與脈衝長度：改成 200 ns

先前的草稿用 100 ns。用 2026-09-27 的資料重算後，建議改成 200 ns。

**線寬是功率展寬。** 同一個視窗（6.97–7.09 GHz、0.5 MHz 一步、走 q2 的線），兩個功率的 f01 線：

| run | 功率 | 線高（1 − P00） | FWHM |
|---|---|---|---|
| 20260927-190114-762 | −30 dBm | 0.31 | ≤ 1.5 MHz（半高以上只有 2 點） |
| 20260927-190222-447 | −20 dBm | 0.37 | 4.6 MHz（擬合） |

- 線寬大約跟振幅（×3.16）一起變。
- −30 dBm 的線高仍有 −20 dBm 的 85%，表示兩個都已經飽和。
- 深度飽和時 FWHM ≈ 2 f_R √(T1/T2)，也就是 f_R = (FWHM / 2) · √(T2 / T1)。

**換算。** T1_c ≈ 5 µs，但 coupler 的 T2 沒量過（idle 在 arch 的陡坡上，約 9 MHz/mV）。
- −30 dBm 仍飽和，所以 T2 ≳ 0.4 µs；上限是 2 T1 = 10 µs。
- 各 run 在 −20 dBm 的 FWHM 是 4.2–8.7 MHz，所以 f_R(−20 dBm) ≈ 0.6–6 MHz。
- port 的 full scale 是 +7 dBm，振幅 a 的功率是 7 + 20 log₁₀ a dBm，所以每單位振幅的 Rabi 約 13–140 MHz。
- cosine（Hann）的 π 需要峰值 Rabi = 1/T：

| 長度 | a_π | 預設掃描 0.25 × (0–1.9) = 0–0.475 |
|---|---|---|
| 100 ns | 0.07–0.8 | T2 短時超出範圍 |
| **200 ns** | **0.04–0.4** | 全部在範圍內 |

**200 ns 的代價很小。**
- coupler 在脈衝中衰減約 4%。
- T2 若只有 0.4 µs，準靜態的頻率漂移（約 0.4 MHz）對 π 造成約 3% 的誤差。
- 頻譜主瓣 ±10 MHz，離 α（−135 MHz）與 q2_q3_c（+98.5 MHz）都很遠，不需要 DRAG。
- 只靠 hybridization（g/Δ ≈ 0.02–0.03）的話，每單位振幅只有 5–9 MHz。實測線寬推得的值比這大，表示 xy2 對
  coupler 還有直接耦合。
- 如果粗掃在 0–0.475 內完全沒有振盪，就是 Rabi 比線寬推得的還慢：把種子提高到 0.5（掃到 0.95），或把脈衝拉到
  400 ns 再掃。

**範圍內可能有好幾個週期**：a_π 小時最多約 12 個半週期。power Rabi 的 estimator 目前取「擬合曲線離零振幅值最遠
的取樣點」（scqat `power_rabi/estimator.py:111`），不是第一個極值。
- 每個奇數極值的理論值一樣大。只有一個週期時沒有差別，qubit 的預設範圍就是這樣。
- 有好幾個週期時，它可能挑到 3π、5π，而且照樣判 SUCCESSFUL。
- 這對 coupler 的第一次 run 是實際的風險，見 §10 決定 5。

### 1.5 訊號、estimator、寫回

**訊號**：
- 模型：P_e(q1) ≈ b + c · P_c(a)，P_c(a) = sin²(π a / 2 a_π)。
- 基線 b 約 0.15：選擇性 π 的失誤加上讀取誤差。
- coupler 被激發的比例 P_c 在 π 時約 0.96（脈衝中衰減 4%）。
- π 時預期 P_e 為 0.65–0.9（s 從 0.67 到 1）。
- 飽和（P_c ≤ 0.5）最多只能到約 0.55，所以 π 時超過 0.55 才算同調翻轉。

**estimator**：`power_rabi` 的模型不動（`population` → `signal`；不衰減的 cosine，振幅與 offset 自由），只有
§10 決定 5 可能改它挑極值的方式。

**寫回**：SUCCESSFUL 時 `xy2.q1_q2_c.pi_amp = 舊值 × 擬合倍率`，預設是待確認的 suggestion（`scqo accept`）。
- 映射只改讀法、不改被量的東西：擬合出的 π 振幅是這條借用 channel 自己的性質，所以照常寫回。這跟
  `flux_component` 不同：那裡掃的是別人的 flux line，量到的不是 target 自己的回應，所以只記錄不寫回。
- 這個值只在 port 目前的 full scale（+7 dBm，與 q2 共用）下有效。永久改 `xy2.q2.drive_power_dbm` 會讓它失準
  （BACKLOG I28），所以 run 的 power context 要記下這個 port 的 full scale（§4）。

## 2. 決策 1：QUAM 怎麼採用借用 channel

### 2.1 限制（state.json；quam 0.5.0、qm-qua 1.2.6）

- **band**：q2 的 xy 在 `con1/6/3`，band 1、`upconverter_frequency` 4.9 GHz、full scale +7 dBm；q1 在 `6/2`，同一組
  port pair。band 1 是 0.05–5.5 GHz，裝不下 7.06 GHz；band 2（4.5–7.5 GHz）同時裝得下 4.9 與 7.1 GHz。
- **upconverter**：一個 MW-FEM port 最多兩個 upconverter（qm-qua `Upconverter = Literal[1, 2]`）。port 的頻率只能
  用 `upconverter_frequency`（只有 upconverter 1）或 `upconverters` 字典兩者之一，兩個都設 qm-qua 會拒絕。
- **quam 的 `MWChannel`**：
  - `upconverter` 欄位會寫進 element 的 `MWInput`。
  - 它的 LO 只在 port 的 `upconverter_frequency` 是 None 時，才讀 `upconverters[upconverter]`。
  - 所以 port 必須整個改用字典。
- **port pair 共用 band**：(2,3)、(4,5)、(6,7)。這是 lab 自己的規則（`broadband_qubit_spectroscopy.py:144-151`），
  vendor 程式裡沒有；各 port 的 LO 可以不同（4、5 就是 5.0 與 4.8 GHz）。
- **沒有現成的欄位**：quam 與 lab 的 class 都沒有裝「多出來的 channel」的欄位。
  - 放在 dict 裡的 channel 照樣會被 `generate_config()` 產生。
  - 但它不在任何 qubit 的 `channels` 裡，qubit 的 `align`、`wait`、thermal reset 都不會碰到它。
  - 沒設 `id` 時，element 名稱就是 dict 的 key。

### 2.2 表示法（建議）

lab 的 root class `MixedTransmonQuam` 加一個欄位 `borrowed_channels: Dict[str, MWChannel]`。
- key 是 SCQO 的位址（`"xy2.q1_q2_c"`），每一項是一個普通的 quam `MWChannel`，`id` 同 key。
- 預設值沿用 class 裡 `qubits` 的寫法：None，`__post_init__` 補成 `{}`。

理由：
- **key 用 SCQO 位址**：vendor config 裡的 mode 名稱本來就跟 roster 一致（q1 = q1），driver 直接用位址查，不必猜。
- **放在 root，不掛在 qubit 或 pair 上**：線不是 qubit（共用一條 xy 線的晶片，一條線上有好幾個 target）。
  微波串擾的 Rabi（`xy2.q1`）也不屬於任何 pair。
- **不用 `extras`**：它沒有型別，裡面的 component 能不能正確序列化沒有保證，也會把 lab 的結構藏起來。
- **相容性**：舊的 state.json 沒有這個欄位，載入後是空的。`to_dict()` 在它是空的時候把它拿掉，所以沒採用過任何
  channel 的 tree 存出來跟以前完全一樣（setup snapshot 也照舊跟 state.json 相等）。只有 `adopt-channel` 寫過之後
  才出現這個 key，從那之後舊版 scqo-qm 就載不了——寫進 release notes。

### 2.3 5Q4C 採用 `xy2.q1_q2_c` 之後

| 位置 | 之前 | 之後 |
|---|---|---|
| port `con1/6/3` 的 band | 1 | 2 |
| port `con1/6/3` 的頻率 | `upconverter_frequency` 4.9 GHz | `upconverters` {1: 4.9 GHz, 2: 7.1 GHz}，`upconverter_frequency` None |
| port `con1/6/2` 的 band | 1 | 2（port pair；LO 4.9 GHz 不動） |
| `borrowed_channels["xy2.q1_q2_c"]` | — | 新的 `MWChannel`（下表） |

| 新 element 的欄位 | 值 |
|---|---|
| `id` | `xy2.q1_q2_c` |
| `opx_output` | `#/ports/mw_outputs/con1/6/3`（直接指 port，wiring.json 不動） |
| `upconverter` | 2 |
| `RF_frequency` | 7056954510.1（`q1_q2_c.f_01_hz`） |
| `intermediate_frequency` | `#./inferred_intermediate_frequency`，推得 −43.05 MHz |
| `core` | 沿用 q2.xy 的 `"b"`（quam：同一個 core 可以給多個 channel 共用），同一個 port 的兩個 element 不多佔資源 |
| `operations` | `x180`、`x90`：`DragCosinePulse`，200 ns，振幅 0.25 / 0.125，`alpha` 0，`anharmonicity` 取 physical.json 的 `anharmonicity_hz`、沒有就 200e6（5Q4C 的 coupler 沒有這個 fact，實際寫入 200e6；DRAG 關閉時不影響波形），`detuning` 0，`axis_angle` 0 |

- LO 選 7.1 GHz 是兩個 coupler 的中間：之後 `xy2.q2_q3_c` 共用同一個 upconverter，IF +55.50 MHz。
- q1、q2 自己的 element 不變（LO 4.9 GHz，IF +244.60 / −57.85 MHz）。產生的 config 除了 band 應該完全相同。
- 換 band 的證據：zz 實驗把這組 port pair 換到 band 2 後，q1 的 x180 `pi_contrast` 仍是 0.89
  （run 20260927-211451-900）。

### 2.4 操作指令 `scqo-qm adopt-channel`

新檔 `scqo_qm/backend/adopt_channel.py`，登錄在 `OPERATOR_COMMANDS`，做法照 `register_partial_swap`：

```
scqo-qm adopt-channel --list                                   # 只看，什麼都不寫
scqo-qm adopt-channel xy2.q1_q2_c --lo-hz 7.1e9 [--dry-run]
```

**預設值**：`--freq-hz` 取 physical.json 的 `<target>.f_01_hz`，`--length-ns` 200，`--pi-amp` 0.25（x90 取一半）。

**`--list`** 列出：
- 每條 drive line 的 port、band、各 upconverter 的 LO、full scale，以及上面的設計 channel。
- 已採用的借用 channel：位址、upconverter、RF、IF、x180/x90 的振幅與長度。

**port 與 band**：
- port 取自這條線的設計 drive channel（`xy2` → `q2.xy.opx_output`）。
- band 取 port 目前的 band，前提是它裝得下兩個 LO；否則取裝得下兩個 LO 的最低 band。
- port pair 的另一個 port 跟著換 band，但它的 LO 必須落在新 band 內，絕不 park。`_coupler_tone` 是每次 run 暫時
  park，這裡是永久設定。

**staged save**（照 `register_partial_swap`）：先存到暫存資料夾，全部通過才 `os.replace` 到 live 的 state.json。
- 寫出的檔案只有 state.json 與 wiring.json，而且 wiring.json 逐位元不變。
- state.json 扣掉 §2.3 那幾處之後，跟 live 的完全相同。
- 在 warnings 當 error 的情況下 `generate_config()` 成功：
  - 新 element 的 `MWInput` 是 (con1, 6, 3)、upconverter 2；
  - q1.xy、q2.xy 的 config 跟之前相同。
- 載入之後磁碟上的 state.json 沒被別人改過。

**拒絕**（SystemExit，檔案一個位元都不動）：
- 位址不是這個 roster 的借用 drive channel（設計 channel、不存在、不是 drive）；
- 這條線沒有設計 drive channel，或它們不在同一個 port；
- port 不是 MW-FEM（Octave 的一個 RF 輸出只有一個 LO，v1 不做）；
- 沒有頻率：沒給 `--freq-hz`，target 也沒有量到的 `f_01_hz`；
- |IF| > 250 MHz（這個 repo 對 MW-FEM 的慣例，`_coupler_tone.MAX_IF_HZ`）；
- upconverter 2 已經用在別的 LO；
- 沒有 band 裝得下兩個 LO，或 port pair 另一個 port 的 LO 裝不下；
- 已經採用過了。v1 沒有 `--update`：振幅、頻率、長度都能用 `scqo set` 改（§2.5）。

**回頭的路**：沒有 `--remove`。
- 每次 run 都會把當時的 QUAM tree 存成 setup snapshot，所以採用前的任何一個 run 都能用
  `scqo restore <run_id> --setup <name>` 重建舊的 setup。
- 上機前另外把 state.json 複製一份（§8 步驟 0）。

### 2.5 driver：用位址找到它

`QMDeviceModel._channel_view` 遇到借用 channel 時交給 `_borrowed_view`，先查 `machine.borrowed_channels.get(name)`：
- 沒有：照舊丟 KeyError，但訊息要寫出採用指令（`scqo-qm adopt-channel xy2.q1_q2_c --lo-hz <Hz>`）。
- 有：回傳新的 view `QMBorrowedDriveChannel`。
- `_realized` 只接 KeyError，所以查找本身不能丟別種例外。

現在的 `QMDriveChannel` 不能直接套用：
- 它的欄位全部透過 `quam_fields`，而 `quam_fields` 寫死走 `qubit.xy`。
- 對一個不是 qubit 的物件，讀會得到 0.0、寫會靜默不做事（`quam_fields.py` 863、885、945、1023、1047）。

作法（實作時定的，比原先規劃的重構小）：
- 不重構 `quam_fields`。那些 helper 只經過 `q.xy.operations`，所以新 view 把採用的 element 包成只有一個屬性
  `xy` 的替身（`_XyHolder`）交給它們。設計 channel 的程式一行都沒動，`test_quam_fields.py` 照舊通過。
- 新 view 讀寫前先檢查 operation（`_require`），不存在就丟錯，不靜默。
- `pi_duration_s` 同時改 `x180` 與 `x90` 的長度：兩者是採用時一起建立的，x90 跟著 x180。

| 欄位 | 借用 channel 上 |
|---|---|
| `drive_freq_hz` | 讀寫 element 的 `RF_frequency`（IF 自動推得）；寫的時候 IF 超過 ±250 MHz 就拒絕 |
| `pi_amp` / `pi_amp_x90` | `x180` / `x90` 的 `amplitude` |
| `pi_duration_s` | `x180`、`x90` 的 `length`（4 ns 格點，照現有規則） |
| `drag_beta` / `drag_beta_x90` | `x180` / `x90` 的 `alpha` |
| `drive_amp`、`drive_power_dbm` | 不支援（NotImplementedError）：沒有 saturation；port 的 full scale 與 q2 共用，寫它會動到 q2（I28） |
| `thermalization_time_s`、`parity_delta_f_hz` | roster 本來就不給借用 channel（它們屬於 target 自己） |

有了這個 view：
- `components()`、`snapshot()`、`scqo state` 會自動看到已採用的借用 channel。`scqo doctor` 不會：它的 CLI 只跑
  roster、design、lock 三類檢查，列出已採用 channel 的 `checks.vendor_checks` 目前只在 API 裡。
- session 開始時會 seed 它的值（`RecordingDevice._seed_pull`）。
- `scqo set xy2.q1_q2_c.pi_amp=…` 能用。

### 2.6 會被影響的既有程式

port 改用 `upconverters` 字典之後，直接讀寫 `port.upconverter_frequency` 的地方，對 6/3 會壞掉：
- `experiments/_coupler_tone.py:78、97、100`（zz、swap 兩個 coupler 光譜）：
  - partner 是 6/3 時會 `float(None)`；
  - tone 打在 6/3 時兩種寫法都會設到，qm-qua 拒絕。
- `experiments/broadband_qubit_spectroscopy.py:114、256、404、424、447`。
- `backend/fieldmap.py:496` 的 VENDOR_ONLY `drive_upconverter_frequency`（只影響顯示）。

作法：
- 新的 `scqo_qm/_mw_fem.py`（放在 package 根目錄，理由同 `_octave.py`）提供 `port_lo(port, upconverter)` 與
  `set_port_lo(port, upconverter, hz)`，兩種寫法都懂，上面每一處都改用它。
- `_coupler_tone`：
  - 只移 tone element 自己的 upconverter（qubit 的 xy 都在 upconverter 1）。
  - 需要換 band、而 port pair 上另一個 upconverter 的 LO 新 band 裝不下時，按名稱拒絕。
  - 5Q4C 採用之後，coupler 的視窗（6.85–7.4 GHz）本來就在 band 2，不需要換 band，upconverter 2 留在 7.1 GHz。
- zz：tone 換 band 時要 park 的 partner 正好是 π member，按名稱拒絕，在建 QUA 之前（BACKLOG I29，這次一起修）。
- broadband：port pair 上有第二個 upconverter（採用過的 channel）時，按名稱拒絕。

## 3. SCQO：兩個 mixin

兩個都做成 capability（catalog 從 mixin 推出來）。之後 `scqo run --capability mapped_readout` 就列得出所有能打
在 coupler 上的實驗。

### 3.1 `drive_line`（`scqo/experiments/_capabilities/drive_line.py`）

`drive_line: str | None = None`。說明文字：
> drive 經過哪條線。None = target 自己的設計 drive channel。給線名時用 channel `<drive_line>.<target>`，可以是
> 借用的（`xy2` 經 q2 的線打 coupler `q1_q2_c`，就是 `xy2.q1_q2_c`）；這條 channel 的 knob（`pi_amp`、
> `drive_freq_hz`…）都在它身上讀寫。

helper：
- `drive_owner(experiment, target)`：有 `drive_line` 時回傳 `"<line>.<target>"`，否則回傳 `target`，走簡寫，
  行為跟今天一模一樣。
- `drive_view(experiment, target)`：`device.channel_on(line, target)` 或 `device.channel(target, "drive")`。

`amplitude.amp_anchor` 改讀 `experiment.anchor(drive_owner(...), field)`。沒有這個欄位的實驗拿到的是 `target`，
行為不變。

Parameters 驗證（v1）：給了 `drive_line` 就只能有一個 target。

### 3.2 `mapped_readout`（`scqo/experiments/_capabilities/mapped_readout.py`）

- `readout_member: str | None = None`。說明文字：
  > 經過哪個 pair member 讀這個 target。target 必須是這個 member 所在 pair 的 coupler：選擇性 π 加 x180 把
  > coupler 的狀態複製到 member 上，再讀 member。None = target 自己的讀取。
- `selective_pi_len_ns: int = 2000`（≥ 16、4 的倍數）：跟 zz 實驗同名同義，共用一段說明文字。

Parameters 驗證（v1，給了 `readout_member` 時）：
- 只能有一個 target，而且不能是 member 本身；
- `use_state_discrimination` 必須是 True：IQ 的軸向投影要用 member 的 blob 中心，v1 不做；
- `reset_method` 必須是 thermal；
- `thermalization_time_ns` 不能設：要等的是 member 的 thermalization，由它自己的 `thermalization_time_s` 決定。

### 3.3 gate：跑之前就拒絕

加在 `Session._validate_targets`，照 `flux_component` 的做法：
- 用名稱讀欄位（session 不 import 實驗）。
- 在配置 run 資料夾之前檢查，所以 run、preview、campaign preflight 都會先擋下。

**`drive_line`**：`want_ops -= {"rx"}`，並檢查：
- 每個 target 的 `<line>.<target>` 必須是 roster 裡的 drive channel。否則沿用 roster 的訊息（「line 'xy9' has no
  channel to 'q1_q2_c' (it reaches: …)」）。
- backend 必須實現它。gate 向 backend 的 vendor device model 要這個 entity，也就是 `components()` 走的同一條路。
  拿到 KeyError 就用它的訊息：QM 會寫出 `scqo-qm adopt-channel`，Qblox 會說它沒有採用任何 channel。

**`readout_member`**：`want_ops -= {"readout"}`，並檢查：
- target 必須是某個 composite 的 coupler；
- member 必須是那個 composite 的 high 或 low；
- member 本身要有 `rx` 與 `readout`。

roster 加一個 helper，由 coupler mode 找出它所在的 composite。

**兩個都沒給**：coupler target 的錯誤訊息要指出下一步，例如：
> q1_q2_c: lacks operation(s) ['readout', 'rx'] — drive it through a line (drive_line=xy1|xy2) and read it
> through a member (readout_member=q1|q2)

### 3.4 `qubit_power_rabi` 的改動

- Parameters 加入兩個 mixin；`description` 加一句。
- `update()` 改成 `drive_view(self, qubit).pi_amp = fit["pi_amp"]`。
- `amp_anchor`（§3.1）讓絕對振幅座標與 `old_pi_amp` 都從借用 channel 讀。
- `simulate()`：給了 `readout_member` 時，population = 0.15 + 0.6 × P_c。亂數抽取的順序不變（先物理，再讀取）。
- `required_operations`、`target_kinds`、contract、estimator 都不變。

## 4. QM：probe

`QMQubitPowerRabi.probe()`：
- 兩個欄位都是 None 時，照舊用 `select_qubits(...)`。
- 否則用 `mapped_targets(self, machine)`，回傳只含一個 handle 的 `BatchableList`。

`build_program` 本身不改。它用到 handle 的 `name`、`xy`、`reset(...)`、`readout_state(...)`，以及
`machine.initialize_qpu(target=…)` 需要的 `z` 與 `align()`。

`MappedTarget`（`scqo_qm/experiments/_mapped_target.py`）：

| 介面 | 有 `readout_member`（coupler） | 只有 `drive_line`（例如微波串擾） |
|---|---|---|
| `name` | target（`q1_q2_c`） | target |
| `xy` | 借用 channel 的 element（經 `_vendor.py` 取得） | 同左 |
| `z` | None（v1 不在 target 的 flux line 上打 pulse） | target 自己的 `z` |
| `reset(...)` | member 的 thermal reset | target 自己的 reset（含 active） |
| `readout_state(state)` | `align` → member 的選擇性 π → member 的 x180 → member 的 `readout_state` | target 自己的 |
| `resonator` | 丟錯（v1 只做 discriminated） | target 自己的 |
| `align()` | 借用 element 加上 member 的所有 channel | 借用 element 加上 target 的 channel |

- `z` 是 None 時，`initialize_qpu` 會略過 settle（`mixed_quam.py:199-203`）；每發開頭 301 µs 的 reset 已經涵蓋
  flux 的穩定時間。
- 選擇性 π 的兩個函式 `pulse_area_ns`、`selective_pi_scale` 從 zz 的 probe 搬到 `experiments/_selective_pi.py`，
  兩邊共用，zz 的行為不變。
- 讀出 member 沒有 `readout_threshold` 時按名稱拒絕，同 zz 的 `refuse_missing_thresholds`。
- **power context**：run record 的 power context 目前對 coupler target 只會記「no readout channel」。要改成記下：
  - `xy2.q1_q2_c` 的 port：`con1/6/3 up2`，full scale +7 dBm；
  - member 的讀取鏈。

  這樣之後才判斷得出這個 `pi_amp` 是在哪個 full scale 下量的。
- 之後的實驗如果用到 handle 還沒有的介面（例如 cryoscope 要在 target 的 flux line 上打 pulse），會在建程式時因
  缺屬性而失敗，還碰不到儀器。到那時再擴充 handle（BACKLOG）。

## 5. Qblox

`QbloxQubitPowerRabi.probe()` 開頭：給了 `drive_line` 或 `readout_member` 就丟 NotImplementedError，按名稱說明
Qblox 還沒有採用任何借用 channel、也沒有映射讀出。
- 這跟 `flux_component` 在三個 Qblox probe 裡的寫法相同，這次補上測試。
- Qblox 的 device model 本來就拒絕借用 channel，所以通常 gate 會先擋下。它的 KeyError 訊息也補上「Qblox 還沒有
  採用任何借用 channel」，gate 轉述的就是這句。
- probe 這一關擋的是 gate 放行的情況：`drive_line` 指向 target 自己的線（設計 channel，device model 認得）。

## 6. 事前拒絕總表

| 在哪裡 | 條件 |
|---|---|
| Parameters | `drive_line` 或 `readout_member` 配多個 target；member 在 targets 裡；映射讀出配 IQ、active reset 或 `thermalization_time_ns` |
| Session gate | 線沒有 channel 到 target；借用 channel 沒被採用；target 不是 member 所在 pair 的 coupler；member 缺 rx 或 readout |
| QM probe | member 沒有 threshold；member 缺 `x180` 或 `saturation`；選擇性 π 換算出來超過 saturation 的振幅 |
| QM 借用 view | 寫 `drive_amp`、`drive_power_dbm`；`drive_freq_hz` 讓 IF 超過 ±250 MHz |
| Qblox probe | 兩個欄位任何一個不是 None |
| `adopt-channel` | 見 §2.4 |

## 7. 測試

**SCQO**（`uv run --extra viewer pytest`，只跑相關的）
- **capability**：`test_capabilities.py` 的 `EXPECTED_CAPABILITIES`、排序清單、欄位說明文字，`test_cli_listing.py`
  的 footer。
- **gate**：§3.3 的每一條拒絕各一個測試；`flux_component` 的 gate 不受影響。
- **端到端**：
  - 在 demo 的 tunable roster 上，先用 `InMemoryDevice.adopt("xy_q1.q0_q1_c", {...})` 採用。要在建 session 之前，
    因為 session 建立時就 seed。
  - 再跑 `qubit_power_rabi` 在 `q0_q1_c` 上（`drive_line="xy_q1"`、`readout_member="q0"`）。
  - 預期 SUCCESSFUL，suggestion 落在 `xy_q1.q0_q1_c.pi_amp`，絕對振幅座標存在。
- **沒採用**：gate 用 driver 的訊息拒絕。
- **回歸**：既有的 `qubit_power_rabi` 測試全部照舊通過。

**scqat**（只有 §10 決定 5 選 (b) 時才動）
- `power_rabi` 的測試加一筆三個週期的資料，確認取到第一個極值。
- 只有一個極值時，結果跟現在相同。

**scqo-qm**（`.venv-qm` 全套）
- **`adopt-channel`**：在 live `quam_state/` 的副本上測，照 `test_register_partial_swap.py`。
  - 只有 §2.3 那幾處改變，wiring 逐位元相同。
  - `generate_config()` 產生正確的 `MWInput`，IF −43.05 MHz；q1、q2 的 config 不變。
  - §2.4 的每一條拒絕都讓檔案逐位元不變。
  - `--list` 和 `--dry-run` 都不寫任何東西。
- **借用 view**：
  - 讀寫落在新 element 上；`drive_amp`、`drive_power_dbm` 按名稱拒絕。
  - 設計 channel 的 `test_quam_fields.py` 照舊。
- **`_borrowed_view`**：
  - 沒採用時是 KeyError，訊息裡有指令名；採用後 `components()`、`snapshot()` 看得到它。
  - 更新 `test_qm_backend.py` 裡「借用 channel 被拒絕」與 snapshot 集合的測試。
- **LO 的兩種寫法**：`_mw_fem` 兩種都讀寫得對。在採用過的 tree 上：
  - `_coupler_tone` 產生的 zz config 只移 upconverter 1，upconverter 2 留在 7.1 GHz；
  - broadband 遇到會讓 upconverter 2 出界的 band 切換，按名稱拒絕。
- **probe**：在採用過的副本上建 `qubit_power_rabi q1_q2_c` 的 QUA，檢查：
  - 借用 element 播 `x180` 並帶 `amplitude_scale`；
  - q1 的 `saturation` 帶 `amp(0.00180)`，`duration` 500；
  - 接著是 q1 的 `x180`，再來是 q1 的讀取；
  - q2 沒有任何 pulse；
  - `qubit_power_rabi q1` 產生的 QUA 跟改之前相同。沒有逐字比對的測試：沒給這兩個欄位時 probe 照舊呼叫
    `select_qubits`，程式本身一字未改；`test_qm_backend.py` 的 builder 對 class 等價測試照舊通過。
- **census**：`test_cli.py`（`OPERATOR_COMMANDS` 多了一項）。

**scqo-qblox**（兩個環境都跑）：兩個欄位的拒絕（含 `drive_line` 指向 target 自己的線）、沒給欄位時照舊
compile，以及 gate 轉述的 KeyError 訊息。

## 8. 上機驗證（5Q4C；每一步都先問你）

遇到第一個硬體或 gateway 錯誤就停下來回報；動過的 knob 全部還原。

0. **看**：`scqo-qm adopt-channel --list`、`scqo state`、`scqo doctor`（採用前的基準），以及步驟 1 的 `--dry-run`。
1. **採用**：先把 `cd2\qm_5q\backend_config\state.json` 複製成同資料夾的 `state.json.pre-adopt-channel.bak`
   （照那裡既有的 `.bak` 慣例；副檔名不是 `.json`，QUAM 載入時不會讀到），再
   `scqo-qm adopt-channel xy2.q1_q2_c --lo-hz 7.1e9`。之後：
   - `scqo doctor` 跟採用前一樣通過（它不列借用 channel，見 §2.5）；
   - `scqo-qm adopt-channel --list` 列出 `xy2.q1_q2_c`：con1/6/3 up2，IF −43.05 MHz；
   - `scqo state` 顯示 `pi_amp` 0.25、`drive_freq_hz` 7.05695 GHz、`pi_duration_s` 2e-7。
2. **回歸檢查**（ports 6/2、6/3 換到了 band 2）：
   - `qubit_power_rabi` 分別打 q1、q2，`--no-update`。π 振幅要在存值的 ±3% 內（0.2141 / 0.2142；2026-09-28 在
     band 1 的 smoke 是 0.2098）。
   - `pair_coupler_spectroscopy_zz`（q1_q2，`--no-update`，約 95 秒）：f01 要在 7.05695 GHz 的一個 FWHM 內。這一步
     驗證改過的 `_coupler_tone`。
3. **預覽**：
   ```
   scqo run qubit_power_rabi --targets q1_q2_c --set drive_line=xy2 --set readout_member=q1 --set use_state_discrimination=true --set num_averages=300 --preview
   ```
4. **粗掃**：同上，但不加 `--preview`、改加 `--no-update`（約 10 秒）。
   - 預期 a = 0 時 P_e 約 0.15，π 時 0.65–0.9；π 振幅 0.04–0.4（倍率 0.16–1.6）。
   - π 時超過 0.55，才表示是同調翻轉而不是飽和（§1.5）。
   - 範圍內出現 2π 時，要看到回到基線。
   - 範圍內不只一個極值時，照 §10 決定 5 處理。
5. **細掃**：把種子設成粗掃的結果（`scqo set xy2.q1_q2_c.pi_amp=…`，要你在對話裡授權），重跑一次。這次範圍內只有
   一個極值，接受它的 suggestion。
6. **閉合**：再跑一次，擬合倍率要在 1.00 ± 0.03 內。
7. **q2_q3**：`scqo-qm adopt-channel xy2.q2_q3_c --lo-hz 7.1e9`（共用 upconverter 2，IF +55.50 MHz），再用
   `readout_member=q3` 走一次步驟 3–6。

## 9. 類比用的既有實驗與證據

- **`pair_coupler_spectroscopy_zz`**：
  - 選擇性 π 的構造（面積、frame detuning、`saturation` × 倍率、`duration`）；
  - 「只看有打 π 的那組」；
  - q1 的讀取在 |0⟩ 時看得到 coupler。
  - 證據：runs 20260927-211451-900、-213623-198、-214341-704。
- **`flux_component`**：
  - 用 Parameters 欄位頂替一個從接線推出來的 operation：session 的 gate 拿掉 `flux_bias`。
  - 「量到的不是 target 自己的性質就不寫回」：這裡相反，量到的是借用 channel 自己的性質，所以寫回。
- **`register_partial_swap`**：改 live state.json 的操作指令。
  - 做法：`--list` 只看、有 `--dry-run`、staged save、拒絕時檔案不動。
  - 測試：用 live state 的副本。
- **`_coupler_tone` 與 `broadband_qubit_spectroscopy`**：MW-FEM 的 band 表、port pair 共用 band、每次 run 暫時搬 LO
  並在 finally 還原。這裡是永久設定，所以不 park。
- **`InMemoryDevice.adopt`**（`scqo/testing.py`）：離線測試裡的採用。

## 10. 要你決定（每一項附建議）

1. **表示法**：root 的 `borrowed_channels`，key 用 SCQO 位址（§2.2）。建議：照這樣做。
2. **5Q4C 的永久設定**：ports 6/2、6/3 從 band 1 換到 band 2；6/3 改用兩個 upconverter，upconverter 2 設 7.1 GHz。
   - 這會動到 q1、q2 的 drive 設定（LO 不變），由 §8 步驟 2 驗證。
   - 建議：做。
3. **映射後補 member 的 x180**（§1.2）。建議：固定補，不另設欄位。
4. **脈衝**：借用 channel 的 `x180`/`x90` 用 200 ns cosine、關閉 DRAG，種子 0.25 / 0.125（§1.4）。
   - 建議：200 ns，而不是草稿的 100 ns。
5. **掃描範圍裡有好幾個週期時怎麼辦**（§1.4）：
   - (a) 只靠操作：粗掃後讀擬合出的週期，把種子換算到 π 再細掃。
   - (b) 另外修 scqat 的 `power_rabi`：在擬合的第一個週期內找極值，不再取全域 argmax。只有一個極值時（qubit 的預設
     範圍）結果不變。
   - 建議：(b)，同時保留 §8 的粗掃 → 細掃。
     - 「第一個極值就是 π」本來就是這個 estimator 文件寫的意圖。
     - 現在的寫法在多週期時會靜默給出 3π，而且判 SUCCESSFUL。
6. **v1 的限制**：
   - 一次 run 只有一個 target；
   - 映射讀出只做 discriminated 與 thermal reset；
   - 只做 QM MW-FEM，Octave 與 Qblox 按名稱拒絕。

   建議：照這樣做。

## 11. 施工與版本

**順序**：scqat（只有決定 5 選 (b) 時才有）→ SCQO → scqo-qm → scqo-qblox，依序落地。最後寫一個 fragment
`RELEASES.d/coupler-transmon.toml`，不切 release。

**各 repo 的工作**：
- **SCQO**：
  - 兩個 capability 模組與 `amp_anchor` 的改動；
  - session gate 與 roster 的 helper；
  - `qubit_power_rabi`；
  - 測試；
  - CLAUDE.md 的 capability 說明、TUTORIAL 加一節「把 coupler 當 qubit 量」。
- **scqo-qm**：
  - `MixedTransmonQuam.borrowed_channels`；
  - `adopt_channel.py` 與它的 `OPERATOR_COMMANDS` 項目；
  - `_mw_fem.py`，以及 `_coupler_tone`、broadband、fieldmap 改用它；
  - `QMBorrowedDriveChannel`（用 `_XyHolder`，不必重構 `quam_fields`，§2.5）、`_borrowed_view`；
  - `_vendor.py` 的借用 channel 入口、`_selective_pi.py`、`_mapped_target.py`；
  - `QMQubitPowerRabi.probe` 與 power context；
  - CLAUDE.md（layout 與借用 channel）。
- **scqo-qblox**：probe 的拒絕、device model 的 KeyError 訊息與測試。

**版本**：MINOR，全部是新增（新欄位都有預設值、新 capability、新 QUAM 欄位、新指令）。release notes 要寫：
- 採用過 channel 的 state.json 帶有 `borrowed_channels`，舊版 scqo-qm 載不了（沒採用的照舊）；
- 採用是操作員的動作，不是升級的必要步驟。
