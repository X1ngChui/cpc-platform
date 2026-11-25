# Dataset conversion examples

The scripts in this directory show how public data become datasets in the platform format (descriptor
`datapackage.yaml` + data file). They are not part of the platform. The data specification is in the design
document, chapter "Data"; the steps are in the guide, chapter "Data preparation".

- Raw files are downloaded to `${CPC_DATA_DIR}/raw/<source>/` and results written to
  `${CPC_DATA_DIR}/<dataset>/` by default (`CPC_DATA_DIR` defaults to `~/.cache/cpc/datasets`); `--raw` uses
  an existing raw file and `--out` sets the output directory.
- Choices made by a conversion are explicit command-line options (e.g. whether requests with 0 output tokens
  are an error or dropped) and are recorded in `cpc.conversion` of the descriptor.
- Datasets are written and validated with `cpc.datasets.build.write_dataset`; `cpc data check <dir>` checks
  them again.

| Script | Kind | Raw data | Licence |
|---|---|---|---|
| `azure_llm.py` | workload | [Azure LLM Inference Trace 2023 / 2024](https://github.com/Azure/AzurePublicDataset) (`AzureLLMInferenceTrace_{conv,code}.csv`, `..._1week.csv`) | CC-BY-4.0 |
| `synthetic_workload.py` | workload | synthetic (non-homogeneous Poisson arrivals), for tests and demonstrations only | - |
| `cn_grid/convert.py` | grid_signal | this directory, `cn_grid/`: provincial time-of-use tariff rules (`tou_tariffs.yaml`, sources per entry) and 2023 provincial average CO2 emission factors of electricity (`carbon_factors_2023.csv`, Ministry of Ecology and Environment and National Bureau of Statistics, Announcement No. 47 of 2025) | public government information |
| `mlenergy.py` | power_profile | [ML.ENERGY Benchmark v3](https://huggingface.co/datasets/ml-energy/benchmark-v3) (`runs/llm.parquet`, gated access, `HF_TOKEN`) | Apache-2.0 |

Measurements of your own servers become power_profile datasets with `cpc measure`.
