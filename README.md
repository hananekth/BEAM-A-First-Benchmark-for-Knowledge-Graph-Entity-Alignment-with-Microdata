# BEAM: A First Benchmark for Knowledge Graph Entity Alignment with Microdata

This repository contains the **code, data, and preprocessing pipeline** for the benchmark introduced in the paper:
**BEAM : Un premier benchmark pour l’alignement des microdonnées du web avec les graphes de connaissances** (📄 link to be added).

The benchmark aligns **Web Data Commons (WDC) macrodata** with **Wikidata** using **key-based matching** (e.g., IATA codes for airports, ISBN for books). Unlike synthetic benchmarks (e.g., DBP15K, OpenEA datasets), MEAB preserves the **noise, heterogeneity, and incompleteness** of real-world data, offering a more realistic evaluation for entity alignment (EA).

Our contributions:

* 🏗️ Provide **class-specific datasets** (currently: *airports* and *books*).
* ⚙️ Include a **preprocessing pipeline** to reproduce or extend the benchmark to new classes.
* 📊 Evaluate several **EA models** (MTransE, AliNet, AlignE, AttrE, GCNAlign, BootEA) under the same hyperparameters as OpenEA.
* 🔗 Supply **ground truth alignments** via *key-based matching* (IATA/ISBN), instead of rare or noisy `owl:sameAs` links.
* 🌐 Release the dataset under FAIR principles — Findable, Accessible, Interoperable, Reusable.

A visualization and navigation tool for exploring the datasets:
🔗 [rust-kg-explorer](https://github.com/bareyan/rust-kg-explorer)

---

## 📦 Repository Structure

```bash
.
├── data/
│   ├── airport/
│   │   ├── rel_triples_1
│   │   ├── rel_triples_2
│   │   ├── attr_triples_1
│   │   ├── attr_triples_2
│   │   └── ent_links
│   └── books/
│       ├── rel_triples_1
│       ├── rel_triples_2
│       ├── attr_triples_1
│       ├── attr_triples_2
│       └── ent_links
│   
├── args/
│   └── *.json   # example configs for running models
├── preprocessing/
│   ├── wdc/         # WDC-specific cleaning
│   ├── wikidata/    # Wikidata extraction & filtering
│   └── entity_linking/
├── scripts/
└── README.md
```

---

## ⚙️ Pipeline Overview

Each major preprocessing step has **two implementations**:

1. ✅ **Original (bash, Python, awk, grep, etc.)** — lightweight and reproducible on any system.
2. 🐘 **Optional PostgreSQL scripts** — for users who prefer relational DB processing at scale, or who like to use the rust-KG explorer tool.

The pipeline includes:

1. **Extract WDC triples** (airports, books).
2. **Clean and filter**: remove irrelevant predicates (e.g., image, logo, hasMap).
3. **Class focus**: enforce typing (`schema.org/Airport`, `schema.org/Book`).
4. **Entity filtering**: drop low-frequency or minimal-information entities.
5. **Key-based merging**: merge duplicates by keys (e.g., IATA, ISBN).
6. **Wikidata extraction**: fetch entities and properties via SPARQL.
7. **Property filtering**: retain high-frequency, relevant attributes.
8. **Entity linking**: generate ground-truth alignment based on keys (IATA/ISBN).

---
Perfect — thanks for clarifying! Since you want to keep the README as you’ve drafted it, but **add the detailed preprocessing steps (Original + SQL)**, we can slot them right after the **Pipeline Overview** section, preserving your style.

Here’s how the continuation should look (you can paste it directly under your “Pipeline Overview” section):

---

## 🔄 Preprocessing Steps

Below we illustrate the main preprocessing stages for **Airports** (Books follow the same logic, with ISBN instead of IATA).
Each step has both the ✅ **original approach** (bash/Python/awk/grep) and the 🐘 **PostgreSQL version**.

---

### 📁 1. Extract Initial Triples

✅ Original:

```bash
python preprocessing/WDC/create_wdc_triples.py 
python preprocessing/WDC/get_wdc_airports.py
```

🐘 PostgreSQL:

```sql
CREATE TABLE wdc_triples_raw (
    subject TEXT,
    predicate TEXT,
    object TEXT
);
COPY wdc_triples_raw FROM '/path/to/wdc_airport_related_triples.txt' DELIMITER E'\t';
```

---

### 📁 2. Clean and Filter (remove logos, images, sameAs, etc.)

✅ Original:

```bash
grep '^_:' wdc_airport_related_triples.txt | \
grep -v '<http://schema.org/image>' | \
grep -v '<http://schema.org/sameAs>' | \
grep -v '<http://schema.org/logo>' > path/to/cleaned/depth0_wdc.txt
```

🐘 PostgreSQL:

```sql
CREATE TABLE wdc_cleaned_1 AS
SELECT * FROM wdc_triples_raw
WHERE subject LIKE '_:%'
  AND predicate NOT IN (
    '<http://schema.org/image>',
    '<http://schema.org/sameAs>',
    '<http://schema.org/logo>'
  );
```

---

### 📁 3. Keep Only Type Airport

✅ Original:

```bash
awk -F'\t' '$2 ~ /type/ && $3 !~ /schema.org\/Airport/ {print $1}' cleaned_depth0_wdc.txt | sort | uniq > bad_ids.txt
grep -vFf bad_ids.txt cleaned_depth0_wdc.txt > cleaned2_depth0.txt
```

🐘 PostgreSQL:

```sql
CREATE TEMP TABLE non_airports AS
SELECT DISTINCT subject
FROM wdc_cleaned_1
WHERE predicate LIKE '%type%'
  AND object NOT LIKE '%schema.org/Airport%';

CREATE TABLE wdc_cleaned_2 AS
SELECT *
FROM wdc_cleaned_1
WHERE subject NOT IN (SELECT subject FROM non_airports);
```

---

### 📁 4. Remove Low-Frequency Entities

✅ Original:

```bash
cut -f1 cleaned2_depth0.txt | sort | uniq -c | sort -n > freq.txt
awk '$1 == 2 {print $2}' freq.txt > min_ids.txt
grep -vFf min_ids.txt cleaned2_depth0.txt > cleaned3_depth0.txt
```

🐘 PostgreSQL:

```sql
CREATE TEMP TABLE subject_counts AS
SELECT subject, COUNT(*) AS freq
FROM wdc_cleaned_2
GROUP BY subject;

CREATE TEMP TABLE min_ids AS
SELECT subject FROM subject_counts WHERE freq = 2;

CREATE TABLE wdc_cleaned_3 AS
SELECT * FROM wdc_cleaned_2
WHERE subject NOT IN (SELECT subject FROM min_ids);
```

---

### 📁 5–10. Further Cleaning

* **Step 5:** remove specific outlier subjects.
* **Step 6:** remove `hasMap` and `mainEntityOfPage`.
* **Step 7:** remove subjects with frequency = 2.
* **Step 8:** remove `schema.org/url`, keep only English names.
* **Step 9:** drop subjects with only 2–3 triples.
* **Step 10:** add more depth if there is linked entities in the same folder.

✅ Each step uses `grep`, `awk`, and filtering commands.
🐘 Equivalent SQL tables are built step-by-step (see paper and scripts for full details).

---

### 📁 11. Wikidata Extraction and Filtering


```bash
python preprocessing/Wikidata/d1_wiki.py  #get triples
python preprocessing/Wikidata/filter_wiki_basedOn_props.py #filter them
python preprocessing/Wikidata/d1_wiki.py #add depth 1 info about the properties in wikidata
```
---

### 📁 12. Entity Linking via Keys


```bash
python preprocessing/entity_linking/get_new_ent_iata_links.py
```

---


### 📁 13. Create training, testing and validation folders


```bash
./scripts/create_folds.sh  
```

---

📌 For the **Books** dataset, replace IATA with **ISBN** keys, but the workflow is identical.

---

## 📊 Dataset Statistics

### Example (Airport benchmark)

| Dataset                 | Attribute Triples | Relational Triples | Links |
| ----------------------- | ----------------- | ------------------ | ----- |
| WDC (airport)           | 6,728             | 28,973             |       |
| Wikidata (airport)      | 61,090            | 163,517            |       |
| Ground truth alignments | –                 | –                  | 2,526 |

Similar statistics are available for the *books* class.

---

## 📈 Model Evaluation

We tested six representative EA models (all from [OpenEA](https://github.com/nju-websoft/OpenEA)):

* **MTransE** (translation-based)
* **AliNet** (multi-hop neighborhood attention)
* **AlignE** (attribute embedding)
* **AttrE** (attribute character embedding)
* **GCNAlign** (graph convolutional alignment)
* **BootEA** (bootstrapping with iterative refinement)

On curated datasets like DBP15K, models reach Hits\@5 of **40–60%**.
On MEAB, performance drops to **\~1–2% Hits\@5**, confirming the difficulty of aligning noisy, semi-structured web macrodata.

---

## 📌 Notes

* Current benchmark covers **Airports** and **Books**; the pipeline generalizes to new classes.
* Ground truth is constructed via **key-based matching** (IATA / ISBN), not `owl:sameAs`.
* Preprocessing deliberately preserves **noise, duplicates, and heterogeneity** to reflect reality.

---

## 📄 License

This project is released under the **CC-BY License**, consistent with the data sources. (to be modified)

---

## ✨ Acknowledgments

We thank the creators of **Web Data Commons** and **Wikidata** for making the data publicly available.
This work is part of and supported by the *mekano* project.

---

## 🔗 Related Projects

* [OpenEA](https://github.com/nju-websoft/OpenEA) – EA model implementations.
* [rust-kg-explorer](https://github.com/bareyan/rust-kg-explorer) – GUI tool for visualizing the datasets.

---

