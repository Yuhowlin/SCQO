# 離線重新估計（`scqo estimate`）＋ 自足的 `dataset.nc` — 實作計畫

狀態：**P1 已落地（2026-09-20）；P2 / P3 未實作**。P1 = §3.1–3.3 的自足 `dataset.nc`
（`Experiment.run_estimate` + `scqo/estimate_inputs.py` + 內嵌快照 + `RunRecord.versions`），
D3 依建議採用。剩下的工作與 §8 的延後項已進 `BACKLOG.md` F11，D1/D2/D4/D5 仍待拍板。
本文件在整個功能落地後刪除（內容移入 TUTORIAL / CLAUDE.md 的對應段落）。

## 0. 要解決的兩件事

1. **看完結果想換分析法，但不想重取數。** 例：`resonator_spectroscopy` 用 `lorentzian` 跑完，想改用
   `circle` 再看一次。
2. **`dataset.nc` 要自足。** 取數當下就把 `estimate()` 需要的 device 值存進 raw data，未來分析不必再去找
   `device_before.json`、setup snapshot、`physical.json` 或（最危險的）現在的 live device。

兩件事是同一個機制的兩面：**讓 `estimate()` 成為「dataset + Parameters」的純函數**。做到這點，重新估計
就只是「換 Parameters 再呼叫一次」。

### 非目標（本次不做）

- 不改任何 scqat estimator；不新增第二個 estimator binding（換 method 是 binding 規則 2，本來就合法）。
- 不做 campaign 統計的重算（子 run 重估後重新 aggregate）。
- 不做批次重估（`--experiment X --since ...`，即「fit 修好後刷新舊 artifacts」的大量版）。
- 不做「沒有 roster 也能分析」。重估仍需要該 device 的 `components.toml`（`components.lock` 的
  superset-by-signature 已保證舊名稱永遠可解析）＋ scqo/scqat 程式碼。
- 不重構 `run()` 成 template method（5 個覆寫者只改一行，見 §3.1）。

## 1. 需要你拍板的決定（都附建議）

| # | 決定 | 建議 | 理由 |
|---|---|---|---|
| D1 | 動詞名稱 | `scqo estimate <run_id>` | 對應 SCQO 層的 `estimate()`（`analyze()` 是 scqat 層的字）。輸入是 run_id 不是實驗名，和 `scqo campaign` 同一個理由不違反「`scqo run` 是唯一入口」 |
| D2 | 衍生 run 的 dataset | **複製**一份 `dataset.nc` | 實測 1961 個 dataset：median 20 KB、p90 408 KB、max 47 MB。複製讓衍生 run 是「普通的 run」：viewer / `open_dataset` / 資料夾搬移全部零修改，且重新蓋上新的內嵌值後它自己也自足 |
| D3 | 內嵌範圍 | **整份** acquisition-time 快照（device knobs+monitors、physical facts、design） | 實測 `device_before.json` median 5.2 KB。「estimator 必要的值」取決於**程式路徑**，而這個功能的目的正是事後換路徑：`resonator_spectroscopy_flux` 的 `dispersive` 讀 `g_hz`/`f_bare_hz`/`ec_hz`…，`sine` 不讀。只存「當時讀過的」會在換 method 時缺值 |
| D4 | 重估時 `update="apply"` | **拒絕**（只准 `suggest` / `none`） | 舊資料的值一律走 `scqo accept <new_run_id>`，讓 era guard 和 staleness guard 把關 |
| D5 | 第一批 estimate-stage 欄位 | 見 §3.5 的清單 | 預設「未標記 = 取數側 = 拒絕覆寫」，所以少標是安全的，之後逐步加 |

## 2. 現況（都已對照程式碼查證）

- 每個 run 已存：`dataset.nc`、`parameters.json`、`result.json`、`device_before.json` / `device_after.json`
  （`RecordingDevice.snapshot()` = knobs + monitors，形狀 `{entity: {field: value}}`）、`record.json`；
  2026-09-03 起另有 setup snapshot（含該 context 的 `scqo_state.json` + `physical.json`）。
  `dataset.nc` 在 `exp.run()` **返回之後**才由 `persist_run` 寫入（`session.py` `run()`），所以
  `estimate()` 前後蓋進 `dataset.attrs` 的東西都會落盤。
- `estimate()` 不碰硬體，但**會讀 live device**，而且有四條路徑，不只 `anchor()`：
  `self.anchor()`、`self.fact()` / `fact_sourced()`、直接 `self.device.channel(q, kind).<field>`
  （`qubit_ramsey.py:153`、`resonator_spectroscopy_power_amp.py:250/254`、`readout_frequency.py:170`、
  `qubit_xyz_delay.py:158/170`…）、以及直接 `experiment.physical.get()` / `experiment.design.get()`
  （`resonator_spectroscopy_flux.py:146/309/312`）。AST 普查：estimate 路徑上共約 60 個讀取點，分佈在
  ~20 個實驗＋共用 helper（`_punchout.coupling_fit`、`flux_anchor_v`、`amp_anchor`…）。
  → **逐實驗宣告「我讀什麼」不可行也不可靠；攔截點必須在 device / physical / design 三個 surface。**
- 這是真實的靜默錯誤來源：`resonator_spectroscopy.py:136` 用 `anchor(q, "readout_freq_hz")` 把 detuning
  還原成絕對頻率。原 run 的 suggestion 一旦被 accept，live 值就移動了；用 live 值重估會把修正量加兩次。
- `update()` 全部寫**絕對值**：AST 普查 `update()`（含 `_punchout.propose_branches`）內對 device view 的
  **讀取為 0 筆**——沒有「讀現值＋增量」的相對寫回，所以對著 live device 重播 `update()` 是安全的。
  （P2 把這個普查固定成一支測試，讓它保持為真。）
- 5 個 core 實驗覆寫 `run()` 並直接呼叫 `self.estimate()`：`broadband_qubit_spectroscopy`、
  `qubit_spectroscopy`、`qubit_spectroscopy_flux_pulse`、`resonator_spectroscopy_power_amp`、
  `resonator_spectroscopy_power_chain`（QM driver 另有 `qubit_spectroscopy_cryoscope` 覆寫 `run()`）。
  → 機制只放在 base `run()` 會被繞過。
- 藏在 instance 上、不在 dataset 裡的 estimate 輸入（重估時是全新 instance，拿不到）：
  `QubitT1Bayesian._t1_prior_s`（`estimate:275`，會 AttributeError）；兩個 punchout 的
  `_top_amps` / `_top_context`（用 `getattr(..., {})` 取，重估時**靜默**少掉圖上的 provenance）。
- 既有先例：`_attach_reference_positions()`、`attach_acquisition_coords()`（7 個實驗）已經在做「取數當下
  寫進 dataset，讓 dataset.nc 離線可重播」；`_campaign_suggestions()`（`session.py:854`）已經示範
  「不 acquire，建 exp → 塞 result → 在 `SuggestionCapture` 下重播 `update()`」。
- `accept()` 已有 era guard（run 的 cooldown/setup 必須等於現在的）＋ staleness guard
  （`before == current`）。
- `DatasetContract.validate()` 容許額外的 variables/coords，不檢查 attrs → 已存的 dataset 重新驗證沒問題。
- `index.sqlite` 是可丟棄快取，`SCHEMA_VERSION`（現在 10）不符就自動重建。
- scqat：只有 `parametric_drive_decoherence` 和 `workflows/ep_pipeline.py` 會複製 `dataset.attrs`。

## 3. 設計

### 3.1 凍結後估計：`Experiment.run_estimate()` 是 `estimate()` 的唯一呼叫點

`SuggestionCapture` 是「包在 `update()` 外面的影子 device：讀穿透、寫變成 suggestion」。這裡加它的鏡像
——**包在 `estimate()` 外面的凍結 surface：讀來自快照、寫一律拒絕**。

```python
# scqo/experiment.py
def run_estimate(self, *, frozen: bool = False) -> Result:
    """The ONE way estimate() is invoked - live run or offline re-estimate."""
    if not frozen:                       # live: snapshot NOW (after acquire + boundary reverts)
        embed(self.dataset, experiment=self.name, params=self.params,
              device=self.device.snapshot(),
              physical=self.physical.values() if self.physical is not None else None,
              design=self.design.values)
    surface = load_frozen(self.dataset, self.device.roster)   # refuses BY NAME if attrs are absent
    live = (self.device, self.physical, self.design)
    self.device, self.physical, self.design = surface.device, surface.physical, surface.design
    try:
        self.result = self.estimate()
    finally:
        self.device, self.physical, self.design = live
        self.inputs_used = surface.reads()       # provenance only
    return self.result
```

關鍵性質：**live run 也是對著「從 dataset 自己的 attrs 重建出來的凍結 surface」做 estimate。**
所以「存進去的」和「分析用到的」不可能不一致——這是由結構保證，不是靠測試保證；而且凍結 surface 若和
`RecordingDevice` 的讀取語意有任何出入，45 個實驗的離線測試當場就會壞，不會等到幾個月後重估才發現。

- base `run()` 的最後一行改成 `return self.run_estimate()`；5 個覆寫者各把 `self.estimate()` 改成
  `self.run_estimate()`（各一行）。
- AST 測試（同 `test_no_experiment_module_imports_two_estimators` 的寫法）：`scqo/experiments/` 內任何地方
  不得出現 `self.estimate()` 呼叫。兩個 driver 各放同一支測試。
- Runtime 保險：`Session.run()` 在 `exp.run()` 之後若發現 dataset 沒有 `scqo_schema` attr，在 result 加
  一行警告（`<Class>.run() bypassed run_estimate(): dataset.nc is not self-contained`）——抓 fork /
  contrib 實驗。
- `estimate()` 內的 device **寫入**被凍結 surface 拒絕並指名（寫入屬於 `update()`）。普查看起來沒有這種
  寫入；若有，離線全套測試會指出來。

### 3.2 `dataset.nc` 內嵌內容（netCDF global attrs，值是 JSON 字串）

| attr | 內容 | 誰蓋 |
|---|---|---|
| `scqo_schema` | `1`（內嵌格式版本；immutable run data 是 no-backward-compat 規則的唯一例外，所以讀取端要認版本） | `run_estimate` |
| `scqo_experiment` | 註冊名稱 | `run_estimate` |
| `scqo_parameters` | `params.model_dump(mode="json")` | `run_estimate` |
| `scqo_device` | `RecordingDevice.snapshot()`：所有 entity 的 knobs + monitors | `run_estimate` |
| `scqo_physical` | `physical.values()`：所有 facts（standalone 測試時為 `null`） | `run_estimate` |
| `scqo_design` | `design.values`（`Design` 是 frozen dataclass，`Design(values)` 可原樣重建） | `run_estimate` |
| `scqo_run` | `{run_id, device, backend, cooldown, setup, started_at, versions{scqo, scqat, driver…}}`；衍生 run 另有 `reestimate_of`、`acquired_at` | `Session`（persist 前） |

- 快照時點 = acquire 結束、boundary revert 之後、`estimate()` 之前——和今天 live 讀取發生的時點完全相同
  （`resonator_spectroscopy_power_amp.py:252` 的註解說的就是這件事）。
- estimate **崩潰**也不影響：attrs 在 `estimate()` 之前就蓋好了，而 `Session.run()` 在失敗路徑仍會
  persist dataset——這正是最需要重估的情況。
- 大小：5Q4C 全部約 10 KB（compact JSON）。風險：HDF5 compact attribute 上限 64 KB；三個 attr 各自遠低於
  此，但 waveform knob（`list[float]`）變多或 20+ qubit 時要注意。加一支 size guard 測試；超過時的退路是
  改成 per-entity attrs（`scqo_schema` 升 2）。
- `_scqat.py` 的 `per_qubit_results` / `whole_dataset_results` 在交給 estimator 前剝掉 `scqo_*` attrs
  （2 行）——scqat 維持「只讀 dataset、不知道 scqo 的內嵌」；也避免 10 KB JSON 被那兩個會複製 attrs 的地方
  帶進 `plotdata.nc`。
- 既有的 per-target 變數（`ref_pos_*`、`digital_amp`…）不動：那些是給 scqat **畫圖/運算**用的物理座標；
  attrs 是給 SCQO 的 `estimate()` **重建 device surface** 用的。兩者用途不同，不合併。

### 3.3 新模組 `scqo/estimate_inputs.py`（`suggestions.py` 的兄弟）

- `embed(dataset, ...)` / `load_frozen(dataset, roster)`：上表的編解碼。值域是有限數、有限數 list、`None`
  （store 本來就拒絕 NaN）；`json.dumps(allow_nan=False)`，萬一丟例外則 scrub + stderr 警告，絕不讓一次
  量測因為 provenance 失敗。
- `FrozenDevice(roster, snapshot)`：`RecordingDevice` **讀取語意**的唯讀雙胞胎——
  `component()` / `channel()` / `resonator_of()` / `.roster` / `snapshot()`；knob 為 `None` 或缺 →
  `KeyError`（與 `_get_knob(strict=True)` 同，`anchor()` 因此照常落到 design fallback）；monitor 缺 →
  `None`；composite 走 `read_knob`，fact 讀取照樣被拒；任何寫入 → 指名拒絕。
  `resonator_of` 的 8 行邏輯抽成 `device.py` 的 module-level helper，兩邊共用。
- `FrozenStore(values)`：只有 `.get(entity, field)`。另有 **unavailable 模式**（見 §3.4）：任何 `.get()`
  丟 `UnavailableInputError`。這個例外**不可以**是 `KeyError` / `AttributeError` 的子類——否則會被
  `anchor()` 的 `except (KeyError, AttributeError)` 吞掉，靜默變成 design fallback。
- 讀取紀錄：每次讀記下 `(source, entity, field)` → `record.json` 的 `estimate_inputs_used`
  （record-only，和 `power_context` 同等級；viewer 可以列「這次 fit 依賴了哪些值」）。

### 3.4 舊 run（沒有內嵌 attrs）的輸入來源

現有的 ~2000 個 run 都沒有 attrs，但你眼前想重估的很可能就是它們。`Session.reestimate()` 對這種 root：

| 來源 | 取自 | 備註 |
|---|---|---|
| device（knobs+monitors） | 該 run 的 `device_before.json` | **所有**既有 run 都有；形狀與 `scqo_device` 相同。是 run START 的值（probe 不改 knob；有 boundary 寫入的實驗都會 revert） |
| physical（facts） | setup snapshot 的 `scqo/physical.json` | 只有 2026-09-03 之後的 run 有。**沒有時 → `FrozenStore` unavailable 模式**：讀到 fact 就指名拒絕，並列出補救：`--input q1_res.g_hz=…` 或 `--facts live`。不可以給空的 store——`fact_sourced()` 會靜默從 measured 降到 design/default，結果和原分析不同卻不報錯 |
| design | live `design.toml` | 舊 run 沒有快照；datasheet 很少變。在 `scqo_run` 標 `design_source: "live"` |

- 以上來源**蓋進衍生 run 的 dataset attrs**，所以舊資料第一次重估後，衍生 run 就是自足的。
- `--input entity.field=value`：操作者明說的值，依 roster 的 role 疊到 device 或 physical 上
  （解析重用 `Session._parse_assignments`，含 `q1.pi_amp → q1_xy` 的 closure 定址）；標記來源 `operator`。
- `--facts live`：明確選擇用現在的 `physical.json`，標記 `facts_source: "live@<時間>"`。
- **永遠不會隱式讀 live device。** 實際讀 fact 的只有 flux 家族（`resonator_spectroscopy_flux` 等），
  所以 unavailable 模式影響面很小；`resonator_spectroscopy` 只讀一個 knob，所有舊 run 都能重估。

### 3.5 estimate-stage 欄位標記

重估只准覆寫「不影響 probe」的欄位；其他欄位指名拒絕（資料已經用那些值取了，改了 record 就說謊）。

```python
# scqo/parameters.py
def estimate_field(default, **kw):          # Field(..., json_schema_extra={"stage": "estimate"})
class Parameters:  # classmethod
    def estimate_fields(cls) -> frozenset[str]: ...   # derived from model_fields
```

- 標記進 JSON schema → `scqo run <name> --help`、`registry.catalog()`（新增 `estimate_fields` 鍵）都看得到。
  AI loop 因此知道哪些旋鈕可以**零成本事後重試**——重取數之前應該先試重估。
- 預設未標記 = 取數側 = 拒絕。**少標安全，多標危險。**
- 防多標的測試（core）：`define_sweep` / `simulate` / `probe` / `run` 的 body 內不得出現
  `self.params.<estimate_field>`。Driver 端同款 AST 測試掃各自的 experiment 模組。
- 第一批（D5），逐一確認過 probe 不讀才標：
  `resonator_spectroscopy`: `analysis_method`, `baseline_order`, `dip_branch` ·
  `resonator_spectroscopy_flux`: `analysis_method`, `dip_method` ·
  `resonator_spectroscopy_power_amp` / `_power_chain`: `dip_method` ·
  `readout_frequency`: `dip_fit_method`, `baseline_order` ·
  `qubit_ramsey`: `ramsey_model` · `qubit_parity_switch_continuous`: `psd_model` ·
  兩個 cryoscope: `fit_start_fractions`, `fit_tau_seeds` · 兩個 broadband: `min_snr`。
  **不標** `reset_method`（取數側）、`depletion_factor`（probe 的 depletion wait 可能會讀，需先查）。

### 3.6 衍生 run 的形狀

衍生 run = 一個普通的 run 資料夾，dataset 來自磁碟而不是儀器。

- `dataset.nc`：root 的複本，重新蓋上新的 `scqo_parameters`、實際使用的 device/physical/design、
  以及 `scqo_run`（含 `reestimate_of`、`acquired_at`）。
- `RunRecord` 新增：`reestimate_of: str = ""`（**永遠指向擁有原始取數的 root**，重估一個衍生 run 時以它的
  parameters 為基底、但 root 不變）、`acquired_at: str = ""`（root 的 `started_at`）、
  `versions: dict`（**所有** run 都記 scqo/scqat 版本——setup snapshot manifest 的版本是去重後第一個 run
  的，回答不了「這個 run 是 Ramsey fix 之前還是之後分析的」）、`estimate_inputs_used`。
- **era 沿用 root 的 cooldown/setup**（`persist_run` 加 `era=` 覆寫）。理由：`accept()` 的 era guard 比的
  是 record 的 era；若蓋今天的 era，上一個 cooldown 的資料今天重估後會**錯誤地通過** guard。
- **不帶 campaign 標記**（否則 `campaign_runs()` 重複計數、統計被污染）。
- `setup_snapshot` 參照 root 的 hash（不新建）；`power_context` 複製 root 的。
- `device_before.json` = `device_after.json` = 重估當下的 live snapshot（suggestion 的 `before` 就是對著它
  算的，這正是這兩個檔對 accept 的意義）；取數當時的值在 dataset attrs 裡。
- tags：繼承 root 的 ＋ 自動 `reestimate` ＋ `--tag`；`seeded:*` 由重播時的 `seed_tags` 自然重生。
- index：`runs.reestimate_of` 欄位 ＋ `find_runs(reestimate_of=)`；`SCHEMA_VERSION` 10 → 11（自動重建）。
- root run **完全不動**。root 若還有 pending suggestion：不自動處理，只在 stderr 提示一行；兩邊提議同一
  欄位時，先 accept 的那個會讓另一個變 stale，staleness guard 自然擋住。

### 3.7 `Session.reestimate(run_id, overrides=None, *, inputs=None, facts="snapshot", update="suggest", tags=None, note="")`

1. `_load_run_record`（既有的 device 歸屬檢查）。root 沒有 `dataset.nc` → 指名拒絕。
2. `registry.get(record["experiment"])`；已不存在的實驗名 → 指名拒絕（release notes 就是遷移路徑）。
3. Parameters = 已存的 `parameters.json` ＋ overrides。**不**再疊 `parameter_defaults`（已存的就是當時的
   有效值，defaults 之後可能改過）。overrides 含非 estimate-stage 欄位 → 指名拒絕並列出合法欄位；
   已存參數過不了現在的 schema → 沿用 `_invalid_params` 的結構化錯誤。
4. 載入 dataset → `Contract.validate()`（失敗指名；RELEASING.md 已把這類破壞定為 MAJOR）。
5. attrs 在 → 直接用；不在 → 依 §3.4 組出來源並 `embed()` 進記憶體中的複本。套用 `--input`。
6. `new_run_dir()`、`artifact_dir`、`exp.run_estimate(frozen=True)`；例外 → `_failure()`。
7. `update != "none"` 且有成功 target → `SuggestionCapture(self.device, self.physical, roster)` 包 **live**
   device 重播 `update()`：`before` = live 現值（staleness guard 才有意義），`after` = 由取數當時 anchor 算出的
   絕對值。`update="apply"` → 指名拒絕（D4）。
8. `persist_run(...)`（§3.6 的新參數）。回傳形狀同 `run()`，另加 `reestimate_of`、`parameters_changed`
   （與 root 的差異）、`inputs`（各來源各用了哪些值）。

### 3.8 CLI：`scqo estimate <run_id> [--set k=v]… [--input entity.field=v]… [--facts snapshot|live] [--no-suggest] [--tag …] [--note …]`

- 新檔 `scqo/cli/estimate.py`，在 `cli/__main__.py` 註冊；`build_session()` 同 `accept` / `suggest`。
- 給它實驗名而不是 run_id → 拒絕並指向 `scqo run <name>`（和 `scqo campaign` 同樣是「檢查過的性質」）。
- `scqo estimate <run_id> --help`：列出該實驗的 estimate-stage 欄位＋已存的值。
- stdout 是和 `scqo run` 同形狀的 JSON（`| jq` 可解析）；提示走 stderr、`#` 前綴。

## 4. 逐檔變更

**SCQO**（`experiment.py`、`session.py` 是 shared core → 需要全套測試）
- 新：`scqo/estimate_inputs.py`、`scqo/cli/estimate.py`、`tests/test_estimate_inputs.py`、
  `tests/test_reestimate.py`、`tests/test_cli_estimate.py`、`RELEASES.d/reestimate.toml`（最後一個 commit 後）
- 改：`scqo/experiment.py`（`run_estimate`）· 5 個 `run()` 覆寫者（各一行）·
  `qubit_t1_bayesian.py`（prior 寫進 dataset，estimate 從 dataset 讀）· 兩個 punchout（`_top_amps` /
  `_top_context` 在 `run()` 內寫進 dataset）· `scqo/_scqat.py`（剝 attrs）· `scqo/device.py`
  （`resonator_of` helper）· `scqo/parameters.py`（`estimate_field`）· §3.5 的 ~10 個 Parameters 類別 ·
  `scqo/experiments/__init__.py`（catalog 的 `estimate_fields`）· `scqo/datastore.py`（RunRecord 欄位、
  `persist_run` 參數、index v11）· `scqo/session.py`（`reestimate`、蓋 `scqo_run`、runtime 保險）·
  `scqo/cli/__main__.py` · viewer run 頁（root ↔ 衍生 run 互相連結，小）
- 文件：CLAUDE.md（package layout 加 2–3 行，不寫 changelog）、TUTORIAL 新段、AGENTS.md / CONTRIBUTING.md
  的 checklist 加一條「`estimate()` 只能透過 `self.device/physical/design` 取 device 值；不得依賴
  `run()`/`probe()` 留在 instance 上的狀態；`run()` 覆寫必須呼叫 `run_estimate()`」；跑
  `python scripts/update_docs.py`。

**scqo-qm**：唯一覆寫 `run()` 的 `qubit_spectroscopy_cryoscope` 已確認是 `try: return super().run()`，
所以不需改；只加兩支 AST 測試。**無 CI → `.venv-qm` 全套本機跑是唯一關卡。**
**scqo-qblox**：只加 AST 測試（普查沒有覆寫）；`uv run` 環境與 `.venv-qblox` 都要綠。
**scqat**：不改，floor 不動。落地順序 scqat（無）→ SCQO → drivers；每個 commit 用明確 pathspec。

## 5. 測試（enforcement 在這裡）

1. **全 registry 往返測試**（本功能的核心保證）：對每個註冊實驗，simulated backend ＋ `tmp_path` datastore
   跑一次 → `reestimate(run_id)` 不帶 override → `fit` / `outcomes` 與原 run 相同（NaN-aware）。接著
   **把 live device 的每個 knob 都改掉、並 accept 原 run 的 suggestions**，再重估一次 → 仍然相同。
   這一支同時抓到：繞過三個 surface 的讀取、藏在 instance 的狀態、`estimate()` 內的寫入。
2. 換 method：`resonator_spectroscopy` lorentzian → `analysis_method="circle"`；`method` 戳記正確、
   COMMON_KEYS 在容差內一致（對應 scqat 的 cross-method 測試）；衍生 record 的 `reestimate_of`、era = root、
   無 campaign 標記、tags。`resonator_spectroscopy_flux` `sine` → `dispersive`（讀更多 fact 的那條路）
   只靠 dataset attrs 就能跑。
3. 指名拒絕：覆寫 `num_averages`、未知欄位、`update="apply"`、root 無 dataset、實驗已不存在、
   舊 run 無 physical 快照時讀 fact（訊息含 entity.field 與兩個補救）、`UnavailableInputError` 不是
   `KeyError`/`AttributeError` 子類。
4. 舊 run 路徑：手工移除 attrs 模擬 pre-feature run → 來源取自 `device_before.json`；衍生 dataset 重估後自足。
5. accept 流程：`accept(derived_id)` 生效且 ChangeRecord 的 `run_id` 是衍生 run；重估與 accept 之間 live 值
   移動 → staleness guard 擋下；跨 cooldown 的衍生 run → era guard 擋下。
6. AST：無 `self.estimate()` 直呼；estimate-stage 欄位不出現在取數側方法；`update()` 內不讀 device 值
   （相對寫回一旦出現，重播就會重複套用）。
7. datastore：index v11、`find_runs(reestimate_of=)`、`campaign_runs()` 不含衍生 run、reindex 可從
   `record.json` 重建新欄位；attrs size guard（demo device < 32 KB）。
8. CLI（subprocess，≤ 5 個 case，這類測試慢）。

指令：局部 `uv run pytest tests/test_estimate_inputs.py tests/test_reestimate.py -q`；落地前
`uv run --extra viewer pytest -q`（shared core，**預期 0 skip**）。

## 6. 分階段

| 階段 | 內容 | 驗收 |
|---|---|---|
| **P1 自足的 dataset** (DONE 2026-09-20) | §3.1–3.3 ＋ `_scqat.py` ＋ `t1_bayesian` / punchout 的 instance 狀態搬進 dataset ＋ `RunRecord.versions` | SCQO 全套綠；任一新 run 的 `dataset.nc` 有全部 `scqo_*` attrs；live 結果與改動前逐位元相同 |
| **P2 動詞** | §3.4–3.8 ＋ 測試 1–8 | 往返測試涵蓋 45/45；對一個**真實的舊 5Q4C `resonator_spectroscopy` run** 做 `--set analysis_method=circle`，肉眼確認圖與絕對頻率 |
| **P3 周邊** | viewer 連結、文件、driver AST 測試、release fragment | 兩個 driver 套件綠；`test_docs_current.py` 綠 |

**P1 應該先落地、而且越早越好**：在它之前取的每一筆資料都沒有內嵌值（只能靠 §3.4 的次級來源）。
P1 本身不依賴 P2，可以獨立 release。

## 7. 風險

- **凍結 surface 與 `RecordingDevice` 讀取語意不一致** → 改變 live 行為。緩解：P1 驗收要求 live 結果逐位元
  不變；45 個實驗的離線測試每次都走凍結路徑。
- **多標 estimate-stage 欄位** → 重估後的 record 說謊。緩解：預設拒絕＋兩端 AST 測試＋第一批逐一人工確認。
- **HDF5 64 KB attribute 上限**（見 §3.2）。
- **Driver 環境的類別解析**：driver venv 裡 `registry.get()` 回傳 driver 子類，其 `estimate()` 是繼承的，
  行為相同；但重估最乾淨的環境是 driver-free 的 `.venv-view` —— 前提是 `build_session()` 在那裡建得起該
  setup 的 backend。**P2 開工時先確認**；若建不起來，`scqo estimate` 需要一條「不建 vendor backend」的
  session 路徑（重估不需要儀器，但 `update()` 重播要 live 的 `scqo_state` / `physical`）。
- **多 agent**：`BACKLOG.md` 目前被另一個 session 修改中（off-limits）；開工前對三個 repo 各跑一次
  `git status`，重疊的檔案走 worktree。

## 8. 延後（落地時寫進 BACKLOG.md）

- Campaign 子 run 重估後的統計重算。
- 批次重估（fit 修好後刷新一段時間內的舊 run）；需要先決定資料夾倍增的政策。
- 2026-09-03 之前的 run：從 `history.sqlite` 以 `started_at` 為準重建當時的 facts（取代 `--facts live`）。
- `design.toml` 納入 setup snapshot。
- 把 5 個 `run()` 覆寫者收斂成 base `run()` ＋ `acquire()` hook（它們全都是「boundary 寫入包住 acquire」，
  而且目前都沒有呼叫 `attach_acquisition_coords()`）。
