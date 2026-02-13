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

A visualization and navigation tool for exploring the datasets:
🔗 [rust-kg-explorer](https://github.com/bareyan/rust-kg-explorer)

---

## 📦 Repository Structure

```bash
.
├── data/                           # 📊 FINAL BENCHMARK DATA (ready to use)
│   ├── airport/
│   │   ├── attr_triples_1          # WDC attribute triples (6,728 triples)
│   │   ├── rel_triples_1           # WDC relational triples (28,973 triples)
│   │   ├── attr_triples_2          # Wikidata attribute triples (61,090 triples)
│   │   ├── rel_triples_2           # Wikidata relational triples (163,517 triples)
│   │   ├── ent_links               # Ground truth entity alignments (2,526 links)
│   │   └── 271_5fold/              # Train/test/valid splits (generated)
│   │       ├── 1/
│   │       │   ├── train_links     # 70% of entity links
│   │       │   ├── test_links      # 20% of entity links
│   │       │   └── valid_links     # 10% of entity links
│   │       ├── 2/ ... 5/           # 5 different random splits
│   └── books/
│       ├── attr_triples_1          # WDC attribute triples (206 triples)
│       ├── rel_triples_1           # WDC relational triples (70 triples)
│       ├── attr_triples_2          # Wikidata attribute triples (573 triples)
│       ├── rel_triples_2           # Wikidata relational triples (651 triples)
│       ├── ent_links               # Ground truth entity alignments (82 links)
│       └── 271_5fold/              # Train/test/valid splits (generated)
│   
├── args/                           # ⚙️ Model configuration files
│   ├── alinet_args.json            # AliNet hyperparameters
│   ├── attre_args.json             # AttrE hyperparameters
│   └── bootea_args.json            # BootEA hyperparameters
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
├── bert_int_integration/           # 🤖 BERT-INT model integration (generated)
│   ├── convert_to_bertint.py       # Convert BEAM format to BERT-INT format
│   ├── run_bertint.py              # Run BERT-INT experiments
│   └── configs/                    # BERT-INT configuration files
│
├── results/                        # 📈 Experiment outputs (generated)
│   ├── airport/                    # Results for airport dataset
│   └── books/                      # Results for books dataset
│
├── requirements.txt                # Python dependencies
├── requirements-dev.txt            # Test dependencies (pytest)
├── tests/                          # Unit/integration tests
├── docs/wikidata_reference.md      # Verified Wikidata class/property IDs for presets
├── Article_BEAM_ACM_SAC26.pdf      # Research paper
└── README.md                       # This file
```

## ✅ Test Commands

```bash
pip install -r requirements-dev.txt
pytest -q
```

## ⚡ Quick Local Class For Fast Build Tests

Create a tiny local class (`TestClass`, 3 parts) linked to real Wikidata city entities:

```bash
python scripts/create_testclass_data.py
```

Then in the web UI use preset:
- `Quick local test (TestClass / language label)`

For a bigger local run (targeting roughly 2–5 minutes depending network/API speed):

```bash
python scripts/create_testclass_large_data.py
```

Then in the web UI use preset:
- `Bigger local benchmark (TestClassLarge / language label)`

Note: this preset enables `force_align=true` so each run recomputes alignment (avoids instant cache-only runs).

If it is still too fast on your machine, increase generated volume:

```bash
python scripts/create_testclass_large_data.py --force --parts 30 --noise-lines-per-part 20000
```

Create multiple local matching-pattern classes (label, identifier, URL, sameAs):

```bash
python scripts/create_matching_test_classes.py --force
```

Then in the web UI use one of:
- `TestClass label matching (name -> rdfs:label)`
- `TestClass identifier matching (eidr -> P2704)`
- `TestClass Wikidata links (url -> P31 city)`
- `TestClass Wikidata links (sameAs -> P31 country)`

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

### Generating Benchmark from Scratch (Advanced)

If you want to reproduce the entire pipeline from raw data:

```bash
# Step 1: Extract WDC triples (requires raw WDC dump file named "parts")
cd preprocessing/WDC
python create_wdc_triples.py      # Creates triples_1.txt
python get_wdc_airports.py        # Creates wdc_airport_related_triples.txt

# Step 2: Extract Wikidata triples (requires SPARQL access)
cd ../Wikidata
python d1_wiki.py                 # Fetch entity labels/descriptions
python check_wiki_props.py        # Analyze property frequencies
python filter_wiki_basedOn_props.py  # Filter properties

# Step 3: Entity linking
cd ../entity_linking
python get_new_ent_iata_links.py  # Match entities via IATA/ISBN

# Step 4: Generate final data files
# (Manual cleaning and organization into data/airport/ and data/books/)

# Step 5: Create folds
cd ../../
bash scripts/create_folds.sh
```

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

### Using BERT-INT Model

BERT-INT is a BERT-based entity alignment model. Integration scripts are provided in `bert_int_integration/`.

```bash
# 1. Clone BERT-INT repository
git clone https://github.com/kosugi11037/bert-int.git

# 2. Convert BEAM format to BERT-INT format
python bert_int_integration/convert_to_bertint.py --dataset airport --fold 1

# 3. Run BERT-INT
cd bert-int
python run_bertint.py --dataset airport --fold 1

# Results will be saved in results/airport/
```

**BERT-INT Data Format Requirements:**
- Entity IDs and names in separate files
- Relation triples in format: `entity1_id \t relation_id \t entity2_id`
- Entity alignment pairs: `source_id \t target_id`

The conversion script (`convert_to_bertint.py`) handles all format transformations automatically.

---

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

The preprocessing pipeline consists of multiple stages to clean and prepare the data:

### WDC Processing

1. **Extract Initial Triples** (`create_wdc_triples.py`)
   - Input: Raw WDC dump file (`parts`)
   - Output: `triples_1.txt` (all extracted triples)
   - Regex-based extraction of subject-predicate-object triples

2. **Filter by Class** (`get_wdc_airports.py`)
   - Input: `triples_1.txt`
   - Output: `wdc_airport_related_triples.txt`
   - Keeps only entities typed as `schema.org/Airport` or `schema.org/Book`

3. **Clean and Filter** (manual/bash scripts)
   - Remove irrelevant predicates (image, logo, sameAs, hasMap, url)
   - Remove low-frequency entities (< 3 triples)
   - Keep only English names
   - Enforce type constraints

### Wikidata Processing

1. **Extract Entities** (`d1_wiki.py`)
   - Input: Wikidata SPARQL endpoint
   - Output: `attribute_wd.txt`, `relational_wd.txt`
   - Fetches entities with IATA/ISBN codes
   - Retrieves labels, descriptions, and properties

2. **Analyze Properties** (`check_wiki_props.py`)
   - Input: `attribute_wd.txt`, `relational_wd.txt`
   - Output: `sorted_wiki_props.json`
   - Counts property frequencies
   - Fetches human-readable labels for properties

3. **Filter Properties** (`filter_wiki_basedOn_props.py`)
   - Input: `sorted_wiki_props.json`, raw triple files
   - Output: `attribute_wd_filtered.txt`, `relational_wd_filtered.txt`
   - Removes low-frequency properties (< threshold)
   - Excludes metadata properties (version, dateModified, sitelinks)

4. **Merge Duplicates** (`merge_wikidata_ents.py`)
   - Input: Filtered triple files
   - Output: Normalized triple files
   - Merges entities with same IATA/ISBN code
   - Canonicalizes entity URIs

### Entity Linking

1. **Key-Based Matching** (`get_new_ent_iata_links.py`)
   - Input: WDC triples (with IATA/ISBN), Wikidata triples (with P238/ISBN)
   - Output: `ent_links`
   - Matches entities based on shared IATA codes or ISBN numbers
   - Creates ground truth alignment pairs

### Fold Generation

1. **Create Splits** (`create_folds.sh`)
   - Input: `ent_links`
   - Output: `271_5fold/{1..5}/{train,test,valid}_links`
   - Randomly shuffles entity links (different seed per fold)
   - Splits: 70% train, 20% test, 10% validation
   - Generates 5 different splits for cross-validation

---

## 🛠️ Troubleshooting

### Common Issues

**Issue: `create_folds.sh` fails with "No such file or directory"**
- Solution: Ensure `data/airport/ent_links` and `data/books/ent_links` exist
- Run: `ls -la data/airport/ent_links` to verify

**Issue: SPARQL queries timeout in `d1_wiki.py`**
- Solution: The script includes retry logic and exponential backoff
- Reduce `BATCH_SIZE` in the script if timeouts persist
- Consider using a local Wikidata dump instead of the public endpoint

**Issue: Out of memory when processing large WDC dumps**
- Solution: `create_wdc_triples.py` processes in chunks (100k lines)
- Increase `chunk_size` parameter if you have more RAM
- Use streaming processing for very large files

**Issue: BERT-INT format conversion fails**
- Solution: Ensure entity IDs are properly extracted
- Check that triple files are tab-separated
- Verify entity links file has correct format

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
- **BERT-INT** for the BERT-based alignment model

This work is part of and supported by the *mekano* project.

---

## 🔗 Related Projects

* [OpenEA](https://github.com/nju-websoft/OpenEA) – Entity alignment model implementations
* [BERT-INT](https://github.com/kosugi11037/bert-int) – BERT-based interaction model for EA
* [rust-kg-explorer](https://github.com/bareyan/rust-kg-explorer) – GUI tool for visualizing the datasets
* [Web Data Commons](http://webdatacommons.org/) – Large-scale web microdata corpus
* [Wikidata](https://www.wikidata.org/) – Free and open knowledge base

---

## 📧 Contact

For questions, issues, or contributions, please open an issue on GitHub or contact the maintainers.
