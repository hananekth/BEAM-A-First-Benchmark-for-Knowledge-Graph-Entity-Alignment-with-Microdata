# BEAM: A First Benchmark for Knowledge Graph Entity Alignment with Microdata

This repository contains the **code, data, and preprocessing pipeline** for the benchmark introduced in the paper:
**BEAM : Un premier benchmark pour l'alignement des microdonnées du web avec les graphes de connaissances** (📄 [Article_BEAM_ACM_SAC26.pdf](Article_BEAM_ACM_SAC26.pdf)).

The benchmark aligns **Web Data Commons (WDC) microdata** with **Wikidata** using **key-based matching** (e.g., IATA codes for airports, ISBN for books). Unlike usual benchmarks (e.g., DBP15K, OpenEA datasets), BEAM preserves the **noise, heterogeneity, and incompleteness** of real-world data, offering a more realistic evaluation for entity alignment (EA).

## 🎯 Key Contributions

* 🏗️ Provide **class-specific datasets** (currently: *airports* and *books*; more classes coming soon).
* ⚙️ Include a **preprocessing pipeline** to reproduce or extend the benchmark to new classes.
* 📊 Evaluate several **EA models** (MTransE, AliNet, AlignE, GCNAlign, BootEA, BERT-INT) under standardized conditions.
* 🔗 Supply **ground truth alignments** via *key-based matching* (IATA/ISBN), instead of rare or noisy `owl:sameAs` links.
* 🌐 Release the dataset under FAIR principles — Findable, Accessible, Interoperable, Reusable.

! New release (05/05/2026) : The tool to generate BEAM Benchmarks (aligning WDC classes with any open source) is online:
🔗 [beam-search-tool](https://beam.lisn.upsaclay.fr/)

For more understanding of how to use the tool:
🔗 [beam-tool-github](https://github.com/billalelhachlaf/BEAM-Benchmark-fork)

A visualization and navigation tool for exploring the datasets:
🔗 [rust-kg-explorer](https://github.com/bareyan/rust-kg-explorer)

---

## 📦 Repository Structure

```bash
.
├── data/                           # 📊 FINAL BENCHMARK DATA (ready to use)
│   ├── airport/
│   │   ├── attr_triples_1          # WDC attribute triples 
│   │   ├── rel_triples_1           # WDC relational triples 
│   │   ├── attr_triples_2          # Wikidata attribute triples 
│   │   ├── rel_triples_2           # Wikidata relational triples 
│   │   ├── ent_links               # Ground truth entity alignments 
│   │   └── 271_5fold/              # Train/test/valid splits 
│   │       ├── 1/
│   │       │   ├── train_links     # 70% of entity links
│   │       │   ├── test_links      # 20% of entity links
│   │       │   └── valid_links     # 10% of entity links
│   │       ├── 2/ ... 5/           # 5 different random splits
│   └── ...
│   
├── args/                           # ⚙️ Model configuration files
│   ├── alinet_args.json            # AliNet hyperparameters
│   ├── attre_args.json             # AttrE hyperparameters
│   └── ....
│
├── preprocessing/                  # 🔧 Scripts to generate benchmark from raw data
│   ├── WDC/                        # Web Data Commons processing
│   │   ├── create_wdc_triples.py   # Extract triples from raw WDC dump
│   │   └── get_wdc_airports.py     # Filter airport/book entities
│   ├── Wikidata/                   # Wikidata extraction and filtering
│   │   ├── d1_wiki.py              # Fetch entity labels/descriptions via SPARQL
│   │   ├── check_wiki_props.py     # Analyze property frequencies
│   │   ├── filter_wiki_basedOn_props.py  # Filter low-frequency properties
│   │   └── merge_wikidata_ents.py  # Merge duplicate entities by IATA/ISBN
│   └── entity_linking/             # Entity alignment generation
│       └── get_new_ent_iata_links.py  # Match WDC ↔ Wikidata via keys
│
├── scripts/
│   └── create_folds.sh             # Generate 5-fold cross-validation splits
│
│
├── results/                        # 📈 Experiment outputs (generated)
│   ├── airport/                    # Results for airport dataset
│   └── books/                      # Results for books dataset
│
├── requirements.txt                # Python dependencies
├── Article_BEAM_ACM_SAC26.pdf      # Research paper
└── README.md                       # This file
```

---

## 📊 Understanding the Data

### Data Flow Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         INPUT DATA                              │
│  (Not included - must be downloaded from WDC and Wikidata)      │
├─────────────────────────────────────────────────────────────────┤
│  • Raw WDC microdata dump (schema.org triples)                  │
│  • Wikidata SPARQL endpoint (query-based extraction)            │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    [preprocessing scripts]
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    INTERMEDIATE DATA                            │
│         (Generated during preprocessing pipeline)               │
├─────────────────────────────────────────────────────────────────┤
│  • triples_1.txt                  - Initial WDC triples         │
│  • wdc_airport_related_triples.txt - Filtered by class          │
│  • attribute_wd.txt               - Raw Wikidata attributes     │
│  • relational_wd.txt              - Raw Wikidata relations      │
│  • attribute_wd_filtered.txt      - Filtered attributes         │
│  • relational_wd_filtered.txt     - Filtered relations          │
│  • sorted_wiki_props.json         - Property frequency analysis │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    [entity linking + cleaning]
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                      FINAL DATA                                 │
│              (data/airport/ and data/books/)                    │
├─────────────────────────────────────────────────────────────────┤
│  • attr_triples_1    - WDC attributes (KG1)                     │
│  • rel_triples_1     - WDC relations (KG1)                      │
│  • attr_triples_2    - Wikidata attributes (KG2)                │
│  • rel_triples_2     - Wikidata relations (KG2)                 │
│  • ent_links         - Ground truth alignments (WDC ↔ Wikidata) │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                      [create_folds.sh]
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                   TRAIN/TEST/VALID SPLITS                       │
│                    (data/*/271_5fold/)                          │
├─────────────────────────────────────────────────────────────────┤
│  • train_links (70%)  - Training entity alignments              │
│  • test_links (20%)   - Testing entity alignments               │
│  • valid_links (10%)  - Validation entity alignments            │
│  • 5 different random splits for cross-validation               │
└─────────────────────────────────────────────────────────────────┘
```

### File Format Specifications

#### Triple Files (attr_triples_*, rel_triples_*)
Tab-separated format: `subject \t predicate \t object`

**Example (WDC - attr_triples_1):**
```
_:n000de465f5b542309b5e84e7cf053549xb1	<http://schema.org/icaocode>	"mtpx"
_:n0015bfc65168484fba11f0be21df9979xb2	<http://schema.org/iatacode>	"waw"
_:n0015bfc65168484fba11f0be21df9979xb2	<http://schema.org/latitude>	"52.170906"
```

**Example (Wikidata - attr_triples_2):**
```
http://www.wikidata.org/entity/q4102	http://www.wikidata.org/prop/direct/p238	"ams"
http://www.wikidata.org/entity/q465071	http://www.wikidata.org/prop/direct/p238	"waw"
```

**Example (rel_triples_1):**
```
_:n0015bfc65168484fba11f0be21df9979xb2	<http://schema.org/address>	_:n0015bfc65168484fba11f0be21df9979xb1
_:n0015bfc65168484fba11f0be21df9979xb2	<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>	<http://schema.org/airport>
```

#### Entity Links File (ent_links)
Tab-separated format: `wdc_entity \t wikidata_entity`

**Example:**
```
_:n0015bfc65168484fba11f0be21df9979xb2	http://www.wikidata.org/entity/q465071
_:n00285882ebe74766bfc797919ecc6827xb0	http://www.wikidata.org/entity/q1333923
```

#### Train/Test/Valid Links
Same format as ent_links, but split into:
- **train_links**: 70% of entity alignments (for model training)
- **test_links**: 20% of entity alignments (for final evaluation)
- **valid_links**: 10% of entity alignments (for hyperparameter tuning)

---

## 🚀 Quick Start

### Prerequisites

```bash
# Install Python dependencies
pip install -r requirements.txt

# For preprocessing from scratch (optional):
# - Download WDC microdata dump from http://webdatacommons.org/structureddata/
# - Access to Wikidata SPARQL endpoint (https://query.wikidata.org/sparql)
```

### Using Pre-Generated Data (Recommended)

The repository already contains the final benchmark data in `data/airport/` and `data/books/`. To use it:

```bash
# 1. Generate train/test/valid splits (5-fold cross-validation)
bash scripts/create_folds.sh

# 2. Verify the splits were created
ls data/airport/271_5fold/1/
# Should show: train_links  test_links  valid_links

# 3. Run EA models (see "Model Evaluation" section below)
```

### Generating Benchmark from Scratch (now you can do it using the BEAM search tool)


---

## 📈 Model Evaluation

### Using OpenEA Framework

The benchmark is compatible with [OpenEA](https://github.com/nju-websoft/OpenEA) models. Example configurations are provided in `args/`.

```bash
# Clone OpenEA repository
git clone https://github.com/nju-websoft/OpenEA.git
cd OpenEA

# Copy BEAM data to OpenEA data directory
cp -r ../data/airport ./data/
cp -r ../data/books ./data/

# Run a model (example: BootEA)
python run/main_from_args.py ../args/bootea_args.json airport 1
```



## 📊 Dataset Statistics

### Airport Benchmark

| Component               | Count     | Description                          |
|-------------------------|-----------|--------------------------------------|
| **WDC Entities**        | ~1,200    | Airport entities from web microdata  |
| **Wikidata Entities**   | ~2,800    | Airport entities from Wikidata       |
| **WDC Attr Triples**    | 6,728     | Attribute triples (KG1)              |
| **WDC Rel Triples**     | 28,973    | Relational triples (KG1)             |
| **Wikidata Attr Triples** | 61,090  | Attribute triples (KG2)              |
| **Wikidata Rel Triples** | 163,517  | Relational triples (KG2)             |
| **Ground Truth Links**  | 2,526     | Entity alignments via IATA codes     |

### Books Benchmark

| Component               | Count     | Description                          |
|-------------------------|-----------|--------------------------------------|
| **WDC Entities**        | ~50       | Book entities from web microdata     |
| **Wikidata Entities**   | ~80       | Book entities from Wikidata          |
| **WDC Attr Triples**    | 206       | Attribute triples (KG1)              |
| **WDC Rel Triples**     | 70        | Relational triples (KG1)             |
| **Wikidata Attr Triples** | 573     | Attribute triples (KG2)              |
| **Wikidata Rel Triples** | 651      | Relational triples (KG2)             |
| **Ground Truth Links**  | 82        | Entity alignments via ISBN codes     |

---

## 🔄 Preprocessing Pipeline Details


---

## 📌 Important Notes

* **Ground Truth Quality**: Entity links are based on key matching (IATA/ISBN), which is more reliable than `owl:sameAs` but may miss some valid alignments.
* **Data Noise**: The benchmark deliberately preserves noise, duplicates, and heterogeneity from real-world web data.
* **Scalability**: The pipeline is designed to extend to new classes (e.g., movies, restaurants, products) by modifying the class filters.
* **Reproducibility**: All preprocessing steps are documented and can be reproduced from raw data sources.


---

## ✨ Acknowledgments

We thank the creators of:
- **Web Data Commons** for making web microdata publicly available
- **Wikidata** for providing a comprehensive knowledge graph
- **OpenEA** for the entity alignment framework

This work is part of and supported by the *mekano* project.

---

## 🔗 Related Projects

* [OpenEA](https://github.com/nju-websoft/OpenEA) – Entity alignment model implementations
* [beam-search-tool](https://beam.lisn.upsaclay.fr/) - Tool for creating your Benchmark from different endpoints
* [rust-kg-explorer](https://github.com/bareyan/rust-kg-explorer) – GUI tool for visualizing the datasets
* [Web Data Commons](http://webdatacommons.org/) – Large-scale web microdata corpus
* [Wikidata](https://www.wikidata.org/) – Free and open knowledge base

---

## 📧 Contact

For questions, issues, or contributions, please open an issue on GitHub or contact the maintainers.

