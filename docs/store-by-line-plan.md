# store 依線重整：規格（4.0.0）

> 狀態：規格待核可（2026-09-28）。核可前不寫程式。實作在 worktree `feature/store-by-line`
> （SCQO、scqo-qm、scqo-qblox）；scqat 不動。

## 0. 已定（使用者 2026-09-27／28）

1. store 的 key 用 roster 的 **line 名**，不用儀器 port。port 留在各 setup 的儀器設定裡，
   由 driver 對應、`scqo doctor` 核對、`scqo state` 顯示。
2. `scqo_state.json` 依「line → 對象」巢狀；gate 參數依「composite → operation」巢狀。兩者
   同一次換完。
3. line 的轉移函數（`flux_offset`、`flux_per_phi0`、distortion taps）放在 `physical.json` 的
   line 底下。
4. 換版時做一次性轉換：值不變，只換位址。程式裡不留讀舊 store 格式的碼（舊 run 的資料例外，
   見 §6）。
5. 先把累積的 7 個 feature 發成 3.15.0；這次單獨發 4.0.0（資料格式破壞，major）。
6. flux 串擾等這次落地後重新規劃；coupler π 脈衝（power Rabi）排在這次之後。

## 1. 為什麼要改

- coupler 沒有自己的 xy 線，可以經任一個鄰居的線打；走哪條線要量了才知道（看哪條需要的
  功率小）。現在的模型要先在 components.toml 宣告 channel，才有地方存參數。
- 量 microwave 串擾時，會經別人的線打 qubit。
- 有些設計讓多顆 qubit 共用一條線。現在每條 channel 各有一份 `idle_flux`、`flux_delay_s`，
  但一條線實際上只有一個 DC offset、一個 delay。
- 巢狀關係被壓扁成名字：`iswap_coupler_flux`（composite → operation）、`flux_per_phi0__q1`
  （多對象的線 → 對象）。QM driver 還要把前者拆回來（`QMQubitPair._split`）。

現成的類比：5Q4C 的 feedline 就是「一條線、五個對象」（`q1_ro`…`q5_ro` 共用 port 6/1）。
這次把同一個結構用到所有的線上，並讓對象不必事先宣告。

## 2. 模型

### 2.1 owner：每個值屬於誰

| owner | 位址 | 例子 | 放哪個檔 |
|---|---|---|---|
| mode | `<mode>` | `q1`、`q1_res`、`q1_q2_c` | physical（facts） |
| composite | `<comp>` | `q1_q2` | physical（facts） |
| operation | `<comp>.<op>` | `q1_q2.iswap` | state（gate knobs） |
| line | `<line>` | `z1`、`zc12`、`xy2`、`feedline` | state（line knobs）、physical（line facts） |
| channel | `<line>.<target>` | `xy1.q1`、`feedline.q1`、`z1.q1`、`xy2.q1_q2_c` | state（knobs、monitors）、physical（facts） |

- channel 的意義不變：一條線上打給某個對象的訊號。改變的是名字：不再產生 `q1_xy`、`q1_ro`、
  `q1_z` 這種名字，改用 `<line>.<target>`。
- 多對象 channel 和 pump 沒有單一對象，用它在 components.toml 宣告的名字當子 key：
  `<line>.<宣告名>`。5Q4C、chipA 都沒有這兩種。
- 值的位址 = owner + 欄位，寫成 `<owner>.<field>`，最後一個點後面是欄位：
  `xy2.q1_q2_c.pi_amp`、`q1_q2.iswap.coupler_flux`、`z1.idle_flux`、`q1.f_01_hz`。
- 核心不變式改成：每個值只記一次，記在擁有它的 owner 底下；巢狀只表示 owner 的路徑，
  不重複、不交叉。
- 在程式裡，檔案是巢狀的，記憶體裡仍是扁平的 `{owner: {field: value}}`，owner 是帶點的
  字串。大多數把 key 當不透明字串的程式因此不用改。

### 2.2 設計的 channel、借用的 channel

- **設計的**：components.toml 的 rider（`[lines.xy1] drive = ["q1"]`）和 `[channels.*]`。
  它們決定 `q1.<field>` 簡寫走哪條、衍生的 operation（`rx`、`readout`、`flux_bias`），以及
  doctor 要求儀器實現哪些 channel。**components.toml 的寫法完全不變。**
- **借用的**：不用宣告。每條有 drive 功能的線（線上至少有一條設計的 drive channel）×
  每個可被 drive 的 mode（DERIVATION 表裡有 drive 列的 kind：transmon、flux_transmon、
  fluxonium、cavity），扣掉設計在那條線上的。5Q4C：xy1–xy5 各 8 個（5 顆 qubit、4 個
  coupler，扣掉那條線自己的 qubit），共 40 個。
- 這次只開放借用 drive。借用 flux（flux 串擾）、借用 readout 等有寫入者時再開。
- 借用 channel 的欄位是 drive 的欄位，扣掉只屬於 mode 自己的兩個：`thermalization_time_s`
  （每顆 qubit 兩發之間的等待）和 `parity_delta_f_hz`（qubit 的 parity 監測值）。這兩個只在
  mode 設計的 drive channel 上有。coupler 沒有設計的 drive channel，所以兩個都沒有。
- 借用 channel 的 knob 要在儀器設定裡有對應的 element 才讀寫得到（「採用」一條路線）。
  採用前讀值會報「沒有值」，寫入會報 driver 的「這條路線還沒採用」。採用的指令屬於 coupler
  π 脈衝那一步，不在這次。

### 2.3 兩個檔換版後的樣子（5Q4C cd2，值取自現在的 store）

```jsonc
// scqo_state.json
{"schema": 4, "values": {
  "xy1":      {"q1": {"drive_freq_hz": 5144604647.2, "pi_amp": 0.2141, "drive_amp": 0.4467,
                      "drive_power_dbm": 0.0, "thermalization_time_s": 3.01e-4,
                      "parity_delta_f_hz": 38651.2}},
  "xy2":      {"q2": {"drive_freq_hz": 4842147999.9, "pi_amp": 0.2142, ...}},
  "feedline": {"q1": {"readout_freq_hz": 5934080000.0, "readout_amp": 0.3133,
                      "readout_threshold": 4.30e-5, "fidelity_g": ..., ...},
               "q2": {...}, "q3": {...}, "q4": {...}, "q5": {...}},
  "z1":       {"idle_flux": 0.260448, "flux_delay_s": 9.42e-8},
  "zc12":     {"idle_flux": 0.16, "flux_delay_s": 9.41e-8},
  ...
}}

// physical.json
{"schema": 4, "values": {
  "q1":       {"f_01_hz": 5144604647.2, "t1_s": 3.01e-5, "f_q_max_hz": 5144604647.2, ...},
  "q1_res":   {"f_dress0_hz": 5934679020.1, "kappa_tot_hz": 3975443.9, ...},
  "q1_q2_c":  {"f_01_hz": 7056954510.1},
  "z1":       {"distortion_amp": [...], "distortion_tau_s": [...],
               "q1": {"flux_offset": 0.260448, "flux_per_phi0": 0.9583}},
  ...
}}
```

之後採用了經 q2 的線打 coupler，`xy2` 底下會多一個 `"q1_q2_c": {"drive_freq_hz": ...,
"pi_amp": ..., "pi_duration_s": ...}`；經 q1 的線的在 `xy1` 底下，兩者並存、各有歷史。
gate 參數在 `"q1_q2": {"iswap": {"coupler_flux": ...}}`。

### 2.4 各 owner 的合法欄位（catalog 的改動）

| owner | knob | monitor | fact |
|---|---|---|---|
| line（帶 flux 功能） | `idle_flux`、`flux_delay_s` | — | `distortion_amp[]`、`distortion_tau_s[]` |
| line（只帶 drive／readout） | — | — | — |
| drive channel | `drive_freq_hz`、`pi_amp`、`pi_amp_x90`、`drag_beta`、`drag_beta_x90`、`pi_duration_s`、`drive_amp`、`drive_power_dbm`；只在設計的：`thermalization_time_s` | 只在設計的：`parity_delta_f_hz` | — |
| readout channel | 現有全部 readout knob | 現有全部 readout monitor | — |
| flux channel | — | — | `flux_offset`、`flux_per_phi0` |
| pump channel | 現有 pump knob | — | — |
| operation | `coupler_flux`、`drive_freq_hz`、`amp`、`rel_phase_rad`、`amp_ratio`、`duration_s`、`vz_high_rad`、`vz_low_rad`、`waveform_dt_s`、`waveform[]` | — | — |
| mode、composite | 不變 | 不變 | 不變 |

`catalog.py`：
- `ChannelKind` 的欄位分成 `line_fields`（只有 flux 有）和 `fields`（channel 的），加一個
  `designed_only` 集合（drive：`thermalization_time_s`、`parity_delta_f_hz`）。
- `OP_KNOBS` 改名 `OPERATION_FIELDS`，後綴直接當欄位名。刪掉 `rider_suffix`、
  `op_knob_fields()`、`ALL_STATIC_FIELDS` 和「operation 名會產生撞名欄位」的檢查。
- 唯一性檢查：所有 channel 欄位、line 欄位、mode 欄位彼此不重名（`q1.<field>` 簡寫靠它）。
  operation 欄位不參加，因為只能明寫 `<comp>.<op>.<field>`；所以 `drive_freq_hz` 可以同時
  是 drive channel 和 operation 的欄位。
- waveform 檢查也涵蓋名字就是 `waveform` 的欄位（要有 `waveform_dt_s`）。
- `__<param>` 文法整個拿掉：多對象 channel 的每對象 fact 改記在 `<line>.<target>` 上。
- flux 的 doc 更新：`idle_flux`、`flux_delay_s` 是線的設定（一條線一個）；coupler 的
  decouple 點是它那條 flux 線的 `idle_flux`（`zc12.idle_flux`）。

### 2.5 簡寫 `q1.<field>` 的解析順序

1. `q1` 自己的欄位（facts）。
2. `q1` 每種 kind 的設計 channel（`xy1.q1`、`feedline.q1`、`z1.q1`）的欄位。
3. 那條設計 channel 所在 line 的 line 欄位，但只在 `q1` 是那條線上同 kind 唯一的設計對象時：
   `q1.idle_flux` → `z1.idle_flux`。共用 flux 線時拒絕，並說要寫線名。
4. `q1` 的 resonator：`q1.f_dress0_hz` → `q1_res`。

- 借用 channel 永遠不走簡寫。`q1_q2_c.pi_amp` 會被拒絕，錯誤訊息列出 `xy1.q1_q2_c` …
  `xy5.q1_q2_c`，已經有值的排前面。這就是「一定要標明走哪條線」的強制點。
- composite 的簡寫只到自己的 facts；gate 參數要寫 `q1_q2.iswap.coupler_flux`。

### 2.6 儀器 port 在哪裡出現

- driver 從儀器設定回報每條 line 接在哪個 port。QM 從線上設計 channel 的 element 得出，例如
  `xy2` → con1/6/3、`feedline` → con1/6/1。
- `scqo state` 依 line 分組，標出 port：`xy2 (con1/6/3)`。
- `scqo doctor` 的 line→port 檢查第一次有資料：同一條線上的所有 channel（設計的，加上已採用
  的借用 channel）必須在同一個 port。現在 QM 沒提供，doctor 只給 WARN。
- 同一顆晶片換到另一台儀器：新 setup 的 `scqo_state.json` 從它自己的儀器設定重新產生，key
  完全一樣，歷史和 trends 可以直接比較。

## 3. 各層的改動

依 2026-09-28 的盤點（store 位址的所有使用處）。

**SCQO 核心**
- `entities.py`：channel 的名字就是位址；lock 的 signature 用位址（目前沒有任何 roster 凍結過）。
- `roster.py`：rider 展開成設計 channel（位址）；列舉借用 channel；`_compile` 產生每個 owner
  的合法欄位；`defaults[(target, kind)]` 指向設計 channel 的位址；`resolve_field` 照 §2.5；
  `spec` 的錯誤訊息不再解析 `<op>_<suffix>`；新增 `owners()`。
- `stores.py`：檔案巢狀，記憶體扁平；schema 4。遇到 schema 3 的檔直接報錯並指向轉換指令，
  **不自動封存**，因為 physical.json 的 facts 不能重新產生（現在的 `_load_v3` 會把沒有對應
  schema 的檔封存）。無法辨認的 key 照現在的規則保留。
- `changes.py`：`entity` 欄改名 `owner`，存位址；meta 記 schema 4，沒轉換過的資料庫一樣報錯。
  `ChangeRecord.kind`：channel 記它的 kind、line 記 `line`、operation 記 `operation`。
- `provenance.py`、`suggestions.py`：key 換成 owner。
- `device.py`：
  - view 分三種：channel view（依 kind，只含 channel 欄位）、line view（flux 線的
    `idle_flux`、`flux_delay_s`）、operation view（`OPERATION_FIELDS` 做成屬性，取代
    `read_knob`／`write_knob`）。
  - `channel(t, kind)` 語意不變（設計 channel）；新增 `line(name)`、`operation(comp, op)`、
    `channel_on(line, target)`（任何 channel，含借用的）。
  - `_seed_pull` 只為儀器有回報的借用 channel 建值，不替 40 個借用 channel 各建一份 None。
  - `_sync_coupled` 的範圍從單一 owner 擴大到同一條線上的所有 owner（§4）。
- `session.py`、`cli/set.py`、`cli/suggest.py`、`cli/_review.py`：位址在**最後一個點**切出欄位
  （現在是第一個點）；`scqo accept --entity` 改成 `--owner`。
- `estimate_inputs.py`、`experiment.py`：凍結的 snapshot 用 owner 位址，`scqo_schema` = 2；讀到
  `scqo_schema` = 1（4.0.0 前的 run）時經 v3 對照表翻譯（§6）；`seeded:` tag 用新位址。
- `report.py`、`checks.py`、`cli/state.py`、`cli/doctor.py`：依 line 分組並顯示 port；
  `--fields` 列出 line、channel、operation 的欄位表（operation 欄位第一次看得到、也有單位）；
  design 與量測值的比對照 owner。
- viewer（`app.py`、`_export.py`、8 個模板、`lab_report/metrics.py`、`campaign_query.py`）：
  owner 位址；`lab_report` 不再用字串拼 `f"{q}_xy"`，改查 roster。
- 實驗：66 處 `device.channel(t, kind)`、21 處 `component(<mode 或 composite>)` 不用改；沒有
  實驗寫 gate 參數。要改的是測試裡斷言 suggestion 名字的地方（`q1_xy` → `xy1.q1`）。

**scqo-qm**
- `qm_backend.py`：`component(owner)`——設計的 drive、readout channel 用現有 view（`q.xy`、
  `q.resonator`）；flux 線用 line view（`q.z` 或 coupler）；flux channel 只有 facts，不需要
  view；借用 channel 在儀器設定有對應 element 才有，沒有就 KeyError；operation 用
  `QMOperation`（刪掉 `_split`）。`snapshot()` 回 `{owner: {field}}`；`components()` 帶
  line→port。
- `fieldmap.py`：`FIELD_BINDINGS` 分 line、channel 兩層；`OP_KNOB_BINDINGS` 改成
  `OPERATION_BINDINGS`（key 字串不變）。
- `apply_distortion.py` 改讀 `physical.get(<line>, "distortion_amp")`；`experiments/_vendor.py`
  改走 roster 的 defaults。
- 測試：`test_scqo_glue.py` 的「每種 kind 的 bindings ∪ unrealized = knobs」改成每 (kind, 層)；
  operation 的釘選；components 的 witness；`test_qm_backend.py` 的 snapshot 與 composite 測試。

**scqo-qblox**
- `qblox_backend.py`、`fieldmap.py`、`apply_distortion.py`、`experiments/_vendor.py` 與測試，
  改法同 QM；operation 照舊不支援（KeyError）。

**文件**
- `docs/greenfield-schema.md`：§1 不變式、§2、§5、§6（兩種壓扁寫法拿掉）、§8 範例、§10／§11。
- `TUTORIAL.md` §10（放置規則的例子：`q1_z.flux_per_phi0` → `z1.q1.flux_per_phi0`；
  「放在發出它的 channel 上」→「放在發出它的線或 channel 上」）。
- `CLAUDE.md` 的 package layout 說明、`AGENTS.md`／`CONTRIBUTING.md` 相關段落；重跑
  `python scripts/update_docs.py`。

## 4. 共用 port 的功率

- 現況：MW-FEM 上寫某個 channel 的 `*_power_dbm`，driver 會換 port 的 full-scale 檔位，同一個
  port 上其他 channel 的實際功率跟著變，而且沒有紀錄。5Q4C 的 5 個 readout 共用 port 6/1
  （full-scale −11 dBm），任何一個 `readout_power_dbm` 設到 −17 dBm 以上就會發生。
- 這次：`_sync_coupled` 重讀同一條線上所有 owner 的 knob，有變的各記一筆 coupled change，
  至少不再無聲。
- 不在這次：driver 改成「port 共用時不動 full-scale、只解振幅」的政策，另記 BACKLOG。

## 5. 一次性轉換

### 5.1 指令

`python scripts/convert_store_v4.py <data_root>`：預設只列出會怎麼搬，加 `--apply` 才寫。
只跟 4.0.0 一起出，下一版刪除。

### 5.2 前提

data root 裡沒有任何 run 或 campaign 帶未決定的 suggestion；有的話列出來並拒絕，要先
accept 或 reject。

### 5.3 每個 context（cooldown／setup）做的事

- `physical.json`、`scqo_state.json`：照 §5.4 的對照表換位址，值原封不動，蓋 schema 4。
- `history.sqlite`：一個 transaction 裡把 `entity` 欄改名 `owner`、照對照表更新 owner 與欄位，
  meta 記 schema 4。筆數不變。
- 寫之前各留一份 `*.v3.bak`。
- 已經是 schema 4 的 context 跳過，可以重跑。
- 不動：run 資料夾、dataset.nc、setup snapshots、campaign 資料夾、components.toml、
  design.toml。index.sqlite 是快取，之後重建。

### 5.4 v3 對照表（一個模組，由 components.toml 算出）

| v3 | 4.0.0 |
|---|---|
| `<t>_xy.<f>`、`<t>_ro.<f>` | `<line>.<t>.<f>` |
| `<t>_z.idle_flux`、`<t>_z.flux_delay_s` | `<line>.idle_flux`、`<line>.flux_delay_s` |
| `<t>_z.flux_offset`、`<t>_z.flux_per_phi0` | `<line>.<t>.<f>` |
| `<t>_z.distortion_amp`、`<t>_z.distortion_tau_s` | `<line>.<f>` |
| `<comp>.<op>_<suffix>` | `<comp>.<op>.<suffix>` |
| 多對象 channel `<name>.<f>__<t>` | `<line>.<t>.<f>` |
| `[channels.<name>]`（單一對象） | `<line>.<target>.<f>` |

對照表以 (owner, 欄位) 為單位：一條 v3 的 flux channel 會拆到 line 和 channel 兩個 owner。

### 5.5 預期數字（這台機器）

- 5Q4C cd2：physical 約 60 個值，其中 14 個換位址（q1_z–q5_z 的 `flux_offset`、
  `flux_per_phi0`，q1_z、q3_z 的兩組 taps）；state 19 個 owner 全部換位址；history 1799 筆
  state、180 筆 physical 換 owner。
- 5Q4C cd1：history 105 筆 state、30 筆 physical。
- chipA：qblox setup 27 筆 state、3 筆 physical；另外兩個 setup 沒有歷史。
- 實驗室 PC 的 data root 在升級時跑同一個指令（4.0.0 的升級步驟）。

## 6. 舊 run

- run 資料夾、dataset.nc、setup snapshot 都不改（不可變資料）。
- 從凍結輸入重新 fit 舊 run：`load_frozen` 讀到 `scqo_schema` = 1 時，經 §5.4 的對照表翻譯。
  不可變資料不受「不留舊格式」的規則限制，所以這張對照表永久保留；轉換指令本身下一版刪除。
  這條路現在確實有人用（離線重新 fit 已存的 5Q4C run）。
- `scqo restore <舊 run>`：還原 4.0.0 前的 setup snapshot 時，用同一張對照表把 store 檔轉成
  4.0.0 格式。
- viewer 顯示舊 run 的 before／after 和 suggestion 時照原樣（舊名字）；跟目前值的比對經對照表。
- 舊 run 上的 `seeded:<舊名>` tag 保留；新 run 用新位址。

## 7. 施工順序

0. 從 main 發 3.15.0（使用者說了才發）。
1. 三個 repo 各開 worktree `feature/store-by-line`。
2. 分階段，每階段結束時它的測試全過：
   - A：catalog、entities、roster（owner、借用 channel、簡寫、錯誤訊息）。
   - B：stores、changes、provenance、suggestions、session、CLI 文法。
   - C：device、estimate_inputs（含 v3 對照表）、experiment 的 anchor、實驗測試。
   - D：scqo-qm、scqo-qblox。
   - E：report、`scqo state`／`scqo doctor`、viewer、lab_report。
   - F：轉換指令、文件、fragment。
3. 驗證（§8）。
4. 合併回 main（SCQO 先，再 drivers）。轉換這台的 `D:\qpu_data_dev`、5Q4C 上機冒煙測試，都等
   使用者同意再做。寫 fragment `RELEASES.d/store-by-line.toml`（kind breaking，升級步驟是跑
   轉換指令）；使用者說了才發 4.0.0。

## 8. 驗證

- 全套測試：SCQO `uv run --extra viewer pytest -q`（0 skip）；scqo-qm 在 `.venv-qm` 全套；
  scqo-qblox 在兩個環境全套。
- 新測試：
  - roster：5Q4C 形狀的 roster 產生 40 個借用 drive channel；設計 channel 的位址；
    `q1.idle_flux` → `z1.idle_flux`；共用 flux 線時拒絕；`q1_q2_c.pi_amp` 被拒且列出路線；
    `q1_q2.iswap.coupler_flux` 合法。
  - stores：巢狀讀寫來回一致；schema 3 的檔被拒，訊息指向轉換指令；未知 key 保留。
  - device：借用 channel 採用前讀值報「沒有值」、寫入報 driver 的「未採用」；同一條線上的
    coupled change 被記錄。
  - estimate_inputs：4.0.0 前真實 run 的凍結輸入重新 fit，結果不變。
  - 轉換：fixture data root（含多對象 channel、operation、taps）轉換後，每個舊 (owner, 欄位)
    的值都在新位址且相等；history 筆數不變；有未決 suggestion 時拒絕；重跑不改任何東西。
- 在 `D:\qpu_data_dev` 的**副本**上跑轉換，逐一比對 5Q4C、chipA 所有的值與歷史筆數。
- 用這台的 5Q4C 資料離線重新 fit 幾個舊 run，結果和原本相同。
- 上機（合併、轉換之後，使用者同意再跑）：5Q4C session 啟動（從儀器讀回）、`scqo doctor`、
  `scqo state`（值跟換版前一致、顯示 port）、`qubit_power_rabi --target q1 --no-update`、
  `single_shot_readout --target q1 --no-update`。

## 9. 對外的影響（寫進 4.0.0 的說明）

- 腳本、notebook 裡的舊名字（`q1_xy.pi_amp`、`q1_z.idle_flux`）不能用了；改用簡寫
  `q1.pi_amp`，或新位址 `xy1.q1.pi_amp`、`z1.idle_flux`。例：`scqat/temp/` 裡幾個
  `scqo set q1_z.idle_flux=…` 的草稿。
- ~~`scqo accept --entity` 改成 `--owner`~~：不改，見 §10.1。
- 直接讀 store 檔的外部程式（例如 scqo-agent 的 fork）要跟著改。

## 10. 實作時的修訂（2026-09-28）

實作中發現、跟上面規格不同的地方，以這一節為準。

1. **`entity` 不改名 `owner`**（§3 的 changes.py 一句、§9 的 `--owner` 作廢）。run 資料夾的
   `record.json` suggestions、`campaign.json`、history 的列都以 `entity` 為 key，而 run 資料不可變；
   改名等於所有讀取端永遠要同時認兩個 key，就是「不留舊格式」要避免的相容層。owner 只是文件
   用語，`scqo accept --entity` 照舊。
2. **轉換指令多一個 `--reject-pending`**（§5.1、§5.2）。這台的 data root 有 452 個 run／campaign、
   共 1875 個未決 suggestion（最早 2026-07-20）。預設仍拒絕，列出最新 15 筆與總數；要保留的先在
   3.x 上 accept，其餘加 `--reject-pending` 一次 reject（走 datastore 自己的上鎖編輯器，index 同步
   更新，comment 註明是這次換版）。
3. **對不到新位址的舊值保留舊名**（§5.3）。roster 已經沒有的 entity／field，或兩個 3.x 值落在同一個
   新位址（例：廣播 coil 每個 target 各存一份 taps，4.0.0 只有 line 一份），以舊名留在新檔、報告裡
   逐筆列出，不刪。history 裡這類列也原樣保留。
4. **operation view 保留 `read_knob`／`write_knob`**（§3 device.py 說要改成屬性）。driver 對每個欄位
   各自對到 gate macro，做不到的逐欄以名字拒絕；泛用的一對方法讓這件事留在一處，改成屬性反而要
   每個 driver 為每個欄位實作一個抽象屬性。
5. **driver 的 fieldmap 不分 line、channel 兩層**（§3 scqo-qm 一節）。欄位名在每個 kind 裡唯一，
   `field_bindings()` 仍是每個 kind 一張表，屬於哪一層由 catalog 決定。`Backend` 新增三個 hook：
   `operation_bindings()`、`operation_unrealized()`、`line_ports()`（`{line: port}`，只供顯示）。
6. **借用 channel 的「採用」還沒有慣例**。兩個 driver 目前對借用 channel 一律 KeyError（未採用），
   `components()`、`snapshot()` 不列。儀器設定裡怎麼表示一個被採用的借用 channel，留給下一步
   coupler power Rabi 的計畫決定。
7. 沒有新增 `roster.owners()`：`roster.entities` 就是全部 owner。新增的是 `roster.closure(mode)`，
   簡寫解析與 `qubit_state`／`scqo state --qubit` 用同一份成員表。
8. §5.5 的實際數字（這台 `D:\qpu_data_dev` 的副本，2026-09-28）：5Q4C cd2 physical 47 個值、14 個
   換位址；state 75 個值（19 個舊 owner）全部換位址；history 2271 列、1979 列換位址（= 1799 state
   ＋ 180 physical），6 列是更早就退役的欄位名（`q1_res.f_r_hz` 等），原樣保留。cd1 history 156 列、
   135 列換位址。chipA qblox history 30 列、27 列換位址。重跑不改任何東西。
