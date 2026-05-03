<div align="center">
  <h1>🍿 Trakt to MyAnimeList (MAL) Migrator</h1>
  <p>An automated Python pipeline that extracts anime from your generic Trakt watch history and resolves exact mappings to safely import into MyAnimeList.</p>
</div>

---

## 📖 Overview

Trakt is fantastic, but it lumps Western shows, movies, and Japanese anime into a single database. MyAnimeList is the gold standard for tracking anime, but it has a fundamentally different tracking structure (where every single season is an isolated "series"). 

This tool intelligently sifts through your existing Trakt JSON exports, filters exclusively for anime content using **TMDB API cross-referencing**, and cascades through a **3-tier resolution pipeline** (Kometa Offline DB → AniList GraphQL → Jikan REST) to accurately match your Trakt IDs to MAL IDs. 

Finally, it generates an exact `mal_import.xml` duplicate of MAL's official import schema, allowing you to instantly populate your MAL profile without risking overwrites.

## ✨ Features

- **Comprehensive Data Ingestion**: Seamlessly processes your entire `Trakt data` folder, reading watch history, watchlists, and user ratings simultaneously.
- **Multi-Status Tracking**: Automatically categorizes entries into `Completed`, `Watching` (for partially finished seasons), and `Plan to Watch` (from watchlists).
- **User Rating Injection**: Intelligently extracts your 1-10 Trakt ratings and applies them across all relevant MAL season entries.
- **Optimized TMDB Classification**: Features a robust movie classification engine that uses aggressive local caching and offline database pre-filtering to handle raw, genre-less API exports efficiently without burning through rate limits.
- **Season Disaggregation**: Native mapping logic that splits Trakt's single continuous TV shows into individually recognized MAL entries for every season.
- **Intelligent API Pipeline**: Includes Rate Limiting and strict Connection Pooling to navigate around blocks:
  1. Offline DB mapping for thousands of titles (`anime_ids.json`).
  2. AniList GraphQL zero-auth resolution.
  3. Jikan REST search routines.
- **Modern Desktop GUI**: Packaged with a beautifully scaled `CustomTkinter` graphical user interface that manages threaded processing.

---

## 🚀 Quick Start Setup

### Prerequisites
- **Python 3.10+**
- A **TMDB API Key** (Free from [TMDB's Developer portal](https://www.themoviedb.org/documentation/api))

### 1. Installation
Clone the repository and install the required dependencies:
```bash
git clone https://github.com/kuzhagan143/Trakt-to-MyAnimeList-MAL-Migrator.git
```
```bash
cd trakt-to-mal
```

Install dependencies needed for logic and UI
```bash
pip install httpx python-dotenv tenacity customtkinter
```

### 2. Prepare Data
Export your data from Trakt and place the entire **`Trakt data`** folder (containing your `watched-movies.json`, `watched-shows.json`, `lists-watchlist.json`, and ratings files) into the project directory.

Next, copy the environment template and insert your API key:
```bash
cp .env.example .env
```
Open `.env` and assign your TMDB token to `TMDB_API_KEY`.

---

## 🖥️ Running the Application

### Option A: Graphical User Interface (Recommended)
Launch the tool's visual interface.
```bash
python -m src.main
```
1. Select the location of your `Trakt data` folder using the "Browse" button.
2. Click **Start Processing** and observe the live log terminal.
3. Your final export will land in the `/output` folder upon completion.

### Option B: Command Line Interface (Headless)
If you prefer running this via a server or purely through bash scripts, the CLI handles all paths smoothly.
```bash
# Basic run using the default Trakt data folder
python -m src.main --cli 

# Detailed logging view
python -m src.main --cli --verbose

# Run with a custom Trakt data folder path
python -m src.main --cli --data-dir "/path/to/Trakt data"
```

---

## 📤 Importing to MyAnimeList

1. After the run succeeds, navigate to the `output/` directory and grab `mal_import.xml`.
2. Navigate to the **[MyAnimeList Import page](https://myanimelist.net/import.php)**.
3. Select **MyAnimeList Import** from the dropdown menu.
4. Upload the `mal_import.xml` file.

> **Safety Design Note**: The tool hardcodes `<update_on_import>1</update_on_import>`. This means that your import will neatly append and update new information and gracefully skip any shows on MAL you've already filled out, avoiding catastrophic overwrites of non-anime data.

---

## 🛠️ Diagnostics and Output Files

The `output/` folder contains extensive logs verifying what the algorithm did:
- `detection_report.json`: Line-by-line justification validating exactly why a specific ID map was accepted, which tier of the resolver finalized it, and includes the mapped user scores.
- `skip_log.json`: A log of every show appropriately bypassed (e.g. Western animation, ongoing uncompleted shows, absent metadata).

