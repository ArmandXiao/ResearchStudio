(() => {
  "use strict";

  const EDITIONS_API_URL = "https://researchstudio.site/daily-paper/api/editions?limit=31";
  const GITHUB_REPOSITORY_API_URL = "https://api.github.com/repos/microsoft/ResearchStudio";
  const CONTENT_REFRESH_INTERVAL_MS = 60000;
  const region = document.querySelector("#paper-region");
  const grid = document.querySelector("#paper-grid");
  const loading = document.querySelector("#loading");
  const empty = document.querySelector("#empty");
  const select = document.querySelector("#edition-select");
  const selectLabel = document.querySelector("#edition-label");
  const olderButton = document.querySelector("#edition-older");
  const newerButton = document.querySelector("#edition-newer");
  const modeButtons = [...document.querySelectorAll("[data-mode]")];
  const sourceLink = document.querySelector("#source-link");
  const searchInput = document.querySelector("#paper-search");
  const sortButtons = [...document.querySelectorAll(".sort-toggle[data-sort]")];
  const streamSentinel = document.querySelector("#paper-stream-sentinel");
  const streamStatus = document.querySelector("#paper-stream-status");
  const template = document.querySelector("#paper-template");
  const viewer = document.querySelector("#paper-viewer");
  const viewerTitle = document.querySelector("#viewer-title");
  const viewerCategory = document.querySelector("#viewer-category");
  const viewerExternal = document.querySelector("#viewer-external");
  const viewerQA = document.querySelector("#viewer-qa");
  const viewerLoading = document.querySelector("#viewer-loading");
  const reelTab = document.querySelector("#reel-tab");
  const paperTab = document.querySelector("#paper-tab");
  const reelPanel = document.querySelector("#reel-panel");
  const paperPanel = document.querySelector("#paper-panel");
  const reelFrame = document.querySelector("#reel-frame");
  const paperFrame = document.querySelector("#paper-frame");
  const pdfFallbackLink = document.querySelector("#pdf-fallback-link");
  const githubStarsBadge = document.querySelector("#github-stars-badge");
  const githubStarsCount = document.querySelector("#github-stars-count");

  let editions = [];
  let activeMode = "weekly";
  let activeSelection = null;
  let activeContext = null;
  let activeRecords = [];
  let selectedWeekly = null;
  let selectedDaily = null;
  let activePaper = null;
  let reelReady = false;
  let paperReady = false;
  let streamObserver = null;
  let streamTimer = null;
  let streamPending = false;
  let visiblePaperLimit = 4;
  let editionsSignature = "";
  let refreshInFlight = null;

  const PAPER_BATCH_SIZE = 4;
  const STREAM_REVEAL_DELAY_MS = 220;
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
  const titleResizeObserver = "ResizeObserver" in window
    ? new ResizeObserver(entries => {
        for (const entry of entries) updateTitleClamp(entry.target);
      })
    : null;
  function setLink(element, url) {
    element.href = url;
  }

  function loadGitHubStars() {
    if (!githubStarsBadge || !githubStarsCount) return;
    fetch(GITHUB_REPOSITORY_API_URL, {
      cache: "default",
      credentials: "omit",
      headers: {"Accept": "application/vnd.github+json"},
      mode: "cors"
    })
      .then(response => {
        if (!response.ok) throw new Error("GitHub stars unavailable");
        return response.json();
      })
      .then(data => {
        const stars = data.stargazers_count;
        if (!Number.isInteger(stars) || stars < 0) return;
        const formatter = new Intl.NumberFormat("en");
        const starOffset = Math.floor(Math.random() * 3) + 1;
        const initialStars = Math.max(0, stars - starOffset);
        githubStarsCount.textContent = formatter.format(initialStars);
        githubStarsBadge.title = `${formatter.format(stars)} GitHub stars`;
        githubStarsBadge.setAttribute(
          "aria-label",
          `ResearchStudio on GitHub, ${formatter.format(stars)} stars`
        );
        githubStarsBadge.hidden = false;
        if (initialStars !== stars) {
          setTimeout(() => {
            githubStarsCount.textContent = formatter.format(stars);
            githubStarsCount.classList.add("is-updated");
            setTimeout(() => githubStarsCount.classList.remove("is-updated"), 500);
          }, 650);
        }
      })
      .catch(error => console.warn("Could not load GitHub stars:", error));
  }

  function githubRepoUrl(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const raw = value.trim();
      const parsed = new URL(
        /^https?:\/\//i.test(raw) ? raw : `https://github.com/${raw.replace(/^\/+/, "")}`
      );
      if (parsed.protocol !== "https:" || !["github.com", "www.github.com"].includes(parsed.hostname.toLowerCase())) {
        return null;
      }
      const parts = parsed.pathname.split("/").filter(Boolean);
      if (parts.length < 2) return null;
      const owner = encodeURIComponent(parts[0]);
      const repo = encodeURIComponent(parts[1].replace(/\.git$/i, ""));
      return `https://github.com/${owner}/${repo}`;
    } catch (_error) {
      return null;
    }
  }

  function compactCount(value) {
    return new Intl.NumberFormat("en-US", {
      notation: "compact",
      maximumFractionDigits: 1
    }).format(value);
  }

  function parseUtcDate(value) {
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isNaN(parsed.valueOf()) ? null : parsed;
  }

  function formatDate(value, options = {}) {
    const parsed = parseUtcDate(value);
    if (!parsed) return value || "Unknown date";
    return new Intl.DateTimeFormat("en-US", {
      day: "numeric",
      month: "short",
      year: "numeric",
      timeZone: "UTC",
      ...options
    }).format(parsed);
  }

  function formatWeek(edition) {
    const start = parseUtcDate(edition?.week_start);
    const end = parseUtcDate(edition?.week_end);
    if (!start || !end) return edition?.week_id || "Unknown date";
    const startLabel = formatDate(edition.week_start, {year: undefined});
    const endLabel = formatDate(edition.week_end, {year: undefined});
    return `${startLabel} – ${endLabel}`;
  }

  function formatDaily(value) {
    return formatDate(value, {year: undefined});
  }

  function editionForWeek(weekId) {
    return editions.find(edition => edition.week_id === weekId) || null;
  }

  function dailyDates() {
    const values = new Set();
    for (const edition of editions) {
      for (const paper of edition.papers || []) {
        if (paper.daily_date) values.add(paper.daily_date);
      }
    }
    return [...values].sort((left, right) => right.localeCompare(left));
  }

  function dailyDatesForWeek(weekId) {
    const edition = editionForWeek(weekId);
    if (!edition) return [];
    return [...new Set((edition.papers || []).map(paper => paper.daily_date).filter(Boolean))]
      .sort((left, right) => right.localeCompare(left));
  }

  function weekForDate(date) {
    return editions.find(edition =>
      (edition.papers || []).some(paper => paper.daily_date === date)
    ) || editions.find(edition =>
      edition.week_start <= date && date <= edition.week_end
    ) || null;
  }

  function selectionValues(mode) {
    return mode === "weekly"
      ? editions.map(edition => edition.week_id).filter(Boolean)
      : dailyDates();
  }

  function populateSelector(mode) {
    select.replaceChildren();
    selectLabel.textContent = "Date";
    const values = selectionValues(mode);
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = mode === "weekly" ? formatWeek(editionForWeek(value)) : formatDaily(value);
      select.appendChild(option);
    }
    select.disabled = values.length === 0;
    return values;
  }

  function recordsFor(mode, key) {
    if (mode === "weekly") {
      const edition = editionForWeek(key);
      if (!edition) return [];
      return (edition.papers || []).map(paper => ({edition, paper}));
    }

    const records = [];
    const seen = new Set();
    for (const edition of editions) {
      for (const paper of edition.papers || []) {
        if (paper.daily_date !== key) continue;
        const identity = paper.token || paper.paper_id;
        if (identity && seen.has(identity)) continue;
        if (identity) seen.add(identity);
        records.push({edition, paper});
      }
    }
    records.sort((left, right) => {
      const votes = (Number(right.paper.hf_upvotes_at_snapshot) || 0)
        - (Number(left.paper.hf_upvotes_at_snapshot) || 0);
      if (votes) return votes;
      return String(left.paper.paper_id).localeCompare(String(right.paper.paper_id));
    });
    return records.map((record, index) => ({
      edition: record.edition,
      paper: {...record.paper, rank: index + 1}
    }));
  }

  function updateEditionNavigation() {
    const values = selectionValues(activeMode);
    const index = values.indexOf(activeSelection);
    olderButton.disabled = index < 0 || index >= values.length - 1;
    newerButton.disabled = index <= 0;
  }

  function contextFor(mode, key) {
    if (mode === "weekly") {
      const edition = editionForWeek(key);
      return {
        mode,
        key,
        edition,
        sourceUrl: edition?.source_url || `https://huggingface.co/papers/week/${encodeURIComponent(key)}`
      };
    }
    return {
      mode,
      key,
      edition: weekForDate(key),
      sourceUrl: `https://huggingface.co/papers/date/${encodeURIComponent(key)}`
    };
  }

  function normalizeSearch(value) {
    return String(value || "")
      .normalize("NFKC")
      .toLocaleLowerCase()
      .replace(/\s+/g, " ")
      .trim();
  }

  function resetPaperStream() {
    if (streamTimer !== null) window.clearTimeout(streamTimer);
    streamTimer = null;
    streamPending = false;
    streamObserver?.unobserve(streamSentinel);
    visiblePaperLimit = "IntersectionObserver" in window
      ? PAPER_BATCH_SIZE
      : Number.POSITIVE_INFINITY;
  }

  function updatePaperStream(matchedCount, displayedCount) {
    const hasMore = displayedCount < matchedCount;
    streamSentinel.hidden = !hasMore;
    streamStatus.textContent = matchedCount
      ? `Showing ${displayedCount} of ${matchedCount} papers.`
      : "No papers to show.";
    streamObserver?.unobserve(streamSentinel);
    if (hasMore && !streamPending) streamObserver?.observe(streamSentinel);
  }

  function ensurePaperStreamObserver() {
    if (!("IntersectionObserver" in window) || streamObserver) return;
    streamObserver = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting) || streamPending || streamSentinel.hidden) return;
      streamPending = true;
      streamObserver.unobserve(streamSentinel);
      streamTimer = window.setTimeout(() => {
        streamTimer = null;
        streamPending = false;
        visiblePaperLimit += PAPER_BATCH_SIZE;
        applyCatalogControls();
      }, reducedMotion?.matches ? 0 : STREAM_REVEAL_DELAY_MS);
    }, {rootMargin: "0px 0px -48px 0px", threshold: 0.01});
  }

  function updateSearchPaperCount(totalCount) {
    const paperCountLabel = `${totalCount} ${totalCount === 1 ? "paper" : "papers"}`;
    searchInput.placeholder = `Search title or arXiv ID (${paperCountLabel})`;
    searchInput.setAttribute(
      "aria-label",
      `Search title or arXiv ID in this edition (${paperCountLabel})`
    );
  }

  function updateTitleClamp(title) {
    if (!title) return;
    const card = title.closest(".paper-card");
    if (!card || card.hidden || !title.clientHeight) {
      card?.classList.remove("has-clamped-title");
      return;
    }
    card.classList.toggle(
      "has-clamped-title",
      title.scrollHeight > title.clientHeight + 1
    );
  }

  function applyCatalogControls({resetStream = false} = {}) {
    if (resetStream) resetPaperStream();
    const query = normalizeSearch(searchInput.value);
    const activeSort = sortButtons.find(button => button.getAttribute("aria-pressed") === "true");
    const sortValue = activeSort?.dataset.sort || "votes-desc";
    const ascending = sortValue.endsWith("-asc");
    const criterion = sortValue.startsWith("date-") ? "date" : "votes";
    const cards = [...grid.querySelectorAll(".paper-card")];
    cards.sort((left, right) => {
      if (criterion === "date") {
        const dateDifference = String(left.dataset.dailyDate || "")
          .localeCompare(String(right.dataset.dailyDate || ""));
        if (dateDifference) return ascending ? dateDifference : -dateDifference;
        const voteDifference = Number(right.dataset.hfVotes) - Number(left.dataset.hfVotes);
        if (voteDifference) return voteDifference;
        return Number(left.dataset.originalOrder) - Number(right.dataset.originalOrder);
      }
      const voteDifference = Number(left.dataset.hfVotes) - Number(right.dataset.hfVotes);
      if (voteDifference) return ascending ? voteDifference : -voteDifference;
      const dateDifference = String(right.dataset.publishedDate || "")
        .localeCompare(String(left.dataset.publishedDate || ""));
      if (dateDifference) return dateDifference;
      return Number(left.dataset.originalOrder) - Number(right.dataset.originalOrder);
    });

    let matchedCount = 0;
    let displayedCount = 0;
    let enteringIndex = 0;
    for (const card of cards) {
      const matches = !query || card.dataset.searchText.includes(query);
      const shouldDisplay = matches && matchedCount < visiblePaperLimit;
      if (matches) matchedCount += 1;
      const wasHidden = card.hidden;
      card.hidden = !shouldDisplay;
      if (shouldDisplay) {
        updateTitleClamp(card.querySelector(".paper-title"));
        displayedCount += 1;
        const poster = card.querySelector(".poster");
        if (!poster.hasAttribute("src")) poster.src = poster.dataset.src;
        if (wasHidden && !reducedMotion?.matches) {
          card.style.setProperty("--stream-order", String(enteringIndex));
          enteringIndex += 1;
          card.classList.remove("is-stream-entering");
          void card.offsetWidth;
          card.classList.add("is-stream-entering");
          card.addEventListener(
            "animationend",
            () => card.classList.remove("is-stream-entering"),
            {once: true}
          );
        } else if (wasHidden) card.classList.remove("is-stream-entering");
      } else {
        card.classList.remove("has-clamped-title");
        card.classList.remove("is-stream-entering");
      }
      grid.appendChild(card);
    }

    const totalCount = cards.length;
    updateSearchPaperCount(totalCount);
    loading.hidden = true;
    empty.textContent = totalCount
      ? "No papers match this search."
      : "No published papers are available for this selection yet.";
    empty.hidden = matchedCount > 0;
    grid.hidden = matchedCount === 0;
    updatePaperStream(matchedCount, displayedCount);
  }

  function updateSortButton(button, active) {
    const value = button.dataset.sort || "votes-desc";
    const ascending = value.endsWith("-asc");
    const isDate = value.startsWith("date-");
    const current = isDate
      ? (ascending ? "oldest first" : "newest first")
      : (ascending ? "least voted first" : "most voted first");
    const next = isDate
      ? (ascending ? "newest first" : "oldest first")
      : (ascending ? "most voted first" : "least voted first");
    const criterion = isDate ? "published date" : "Hugging Face votes";
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.title = isDate
      ? `${ascending ? "Oldest" : "Newest"} published first`
      : `${ascending ? "Least" : "Most"} voted first`;
    button.setAttribute(
      "aria-label",
      active
        ? `Sorted by ${criterion}, ${current}. Activate for ${next}.`
        : `Sort by ${criterion}, ${current}.`
    );
  }

  function setSortState(value) {
    const criterion = value.startsWith("date-") ? "date-" : "votes-";
    const target = sortButtons.find(button => button.dataset.sort?.startsWith(criterion));
    if (!target) return;
    target.dataset.sort = value;
    for (const button of sortButtons) updateSortButton(button, button === target);
  }

  function handleSortClick(event) {
    const button = event.currentTarget;
    const active = button.getAttribute("aria-pressed") === "true";
    let value = button.dataset.sort || "votes-desc";
    if (active) value = value.endsWith("-asc")
      ? value.replace(/-asc$/, "-desc")
      : value.replace(/-desc$/, "-asc");
    setSortState(value);
    applyCatalogControls({resetStream: true});
  }

  function renderRecords(records, context, preservedVisibleLimit = null) {
    resetPaperStream();
    if (preservedVisibleLimit !== null) {
      visiblePaperLimit = Math.max(PAPER_BATCH_SIZE, preservedVisibleLimit);
    }
    titleResizeObserver?.disconnect();
    grid.replaceChildren();
    sourceLink.href = context.sourceUrl;
    activeRecords = records;
    activeContext = context;

    for (const [recordIndex, {edition, paper}] of records.entries()) {
      const fragment = template.content.cloneNode(true);
      const card = fragment.querySelector(".paper-card");
      card.hidden = true;
      card.dataset.originalOrder = String(recordIndex);
      card.dataset.searchText = normalizeSearch(`${paper.title} ${paper.paper_id}`);
      card.dataset.paperId = paper.paper_id;
      card.dataset.publishedDate = paper.published_date || paper.daily_date || "";
      card.dataset.dailyDate = paper.daily_date || "";

      const hfVotes = Number(paper.hf_upvotes_at_snapshot) || 0;
      card.dataset.hfVotes = String(hfVotes);
      const hfScore = fragment.querySelector(".hf-score");
      hfScore.querySelector(".hf-vote-count").textContent = hfVotes.toLocaleString("en-US");
      hfScore.setAttribute(
        "aria-label",
        `${hfVotes} Huggingface votes; open this paper on Hugging Face`
      );
      setLink(
        hfScore,
        paper.hf_url || `https://huggingface.co/papers/${encodeURIComponent(paper.paper_id)}`
      );

      const arxivChip = fragment.querySelector(".arxiv-chip");
      arxivChip.querySelector(".paper-id").textContent = paper.paper_id;
      arxivChip.setAttribute("aria-label", `Open ${paper.paper_id} on arXiv`);
      setLink(
        arxivChip,
        paper.arxiv_url || `https://arxiv.org/abs/${encodeURIComponent(paper.paper_id)}`
      );

      const githubChip = fragment.querySelector(".github-chip");
      const githubUrl = githubRepoUrl(paper.github_repo);
      const githubStarValue = paper.github_stars_at_snapshot;
      const githubStars = githubStarValue === null || githubStarValue === undefined
        ? Number.NaN
        : Number(githubStarValue);
      if (githubUrl && Number.isFinite(githubStars) && githubStars >= 0) {
        githubChip.href = githubUrl;
        githubChip.hidden = false;
        githubChip.querySelector(".github-star-count").textContent = compactCount(githubStars);
        githubChip.setAttribute(
          "aria-label",
          `Open the paper repository on GitHub; ${githubStars.toLocaleString("en-US")} stars`
        );
      }
      const displayDate = paper.daily_date;
      const publishedTime = fragment.querySelector(".paper-published");
      publishedTime.dateTime = displayDate;
      publishedTime.textContent = `Published on ${formatDaily(displayDate)}`;
      const paperTitle = fragment.querySelector(".paper-title");
      paperTitle.textContent = paper.title;
      fragment.querySelector(".paper-title-tooltip").textContent = paper.title;
      const poster = fragment.querySelector(".poster");
      poster.dataset.src = paper.poster_url;
      poster.alt = `Preview for ${paper.title}`;
      setLink(fragment.querySelector(".poster-link"), paper.reel_url);

      for (const opener of fragment.querySelectorAll("[data-open-view]")) {
        opener.addEventListener("click", event => {
          event.preventDefault();
          openViewer(edition, paper, opener.dataset.openView);
        });
      }
      grid.appendChild(fragment);
      titleResizeObserver?.observe(paperTitle);
    }

    applyCatalogControls();
    region.setAttribute("aria-busy", "false");
  }

  function writeSelectionUrl(clearViewer = true) {
    const url = new URL(window.location.href);
    url.searchParams.set("mode", activeMode);
    if (activeMode === "weekly") {
      if (activeSelection) url.searchParams.set("week", activeSelection);
      else url.searchParams.delete("week");
      url.searchParams.delete("date");
    } else {
      if (activeSelection) url.searchParams.set("date", activeSelection);
      else url.searchParams.delete("date");
      url.searchParams.delete("week");
    }
    if (clearViewer) {
      url.searchParams.delete("paper");
      url.searchParams.delete("view");
    }
    history.replaceState({}, "", url);
  }

  function renderSelection(mode, key, updateUrl = true, preservedVisibleLimit = null) {
    searchInput.value = "";
    activeMode = mode;
    activeSelection = key;
    select.value = key;
    if (mode === "weekly") selectedWeekly = key;
    else selectedDaily = key;
    updateEditionNavigation();
    renderRecords(recordsFor(mode, key), contextFor(mode, key), preservedVisibleLimit);
    if (updateUrl) writeSelectionUrl();
  }

  function switchMode(mode, updateUrl = true, preferredKey = null) {
    activeMode = mode === "daily" ? "daily" : "weekly";
    for (const button of modeButtons) {
      button.setAttribute("aria-pressed", button.dataset.mode === activeMode ? "true" : "false");
    }
    const values = populateSelector(activeMode);
    const remembered = activeMode === "weekly" ? selectedWeekly : selectedDaily;
    const key = values.includes(preferredKey)
      ? preferredKey
      : values.includes(remembered)
        ? remembered
        : values[0] || null;
    if (key) {
      renderSelection(activeMode, key, updateUrl);
      return;
    }
    activeSelection = null;
    searchInput.value = "";
    updateEditionNavigation();
    renderRecords([], contextFor(activeMode, ""));
    if (updateUrl) writeSelectionUrl();
  }

  function stepEdition(offset) {
    const values = selectionValues(activeMode);
    const index = values.indexOf(activeSelection);
    const nextKey = values[index + offset];
    if (nextKey) renderSelection(activeMode, nextKey);
  }

  function paperPdfUrl(url) {
    const parsed = new URL(url, window.location.href);
    parsed.hash = "view=FitH";
    return parsed.href;
  }

  function showView(view, updateUrl = true) {
    if (!activePaper) return;
    const paperMode = view === "paper";
    reelTab.setAttribute("aria-selected", paperMode ? "false" : "true");
    paperTab.setAttribute("aria-selected", paperMode ? "true" : "false");
    reelPanel.hidden = paperMode;
    paperPanel.hidden = !paperMode;
    viewerLoading.hidden = paperMode ? paperReady : reelReady;
    viewerLoading.textContent = paperMode ? "Loading paper…" : "Loading Reel…";
    if (paperMode && paperFrame.src === "about:blank") paperFrame.src = paperPdfUrl(activePaper.pdf_url);
    viewerExternal.href = paperMode ? paperPdfUrl(activePaper.pdf_url) : activePaper.reel_url;
    if (updateUrl) setPermalink(paperMode ? "paper" : "reel");
  }

  function setPermalink(view) {
    writeSelectionUrl(false);
    const url = new URL(window.location.href);
    url.searchParams.set("paper", activePaper.paper_id);
    url.searchParams.set("view", view);
    history.replaceState({}, "", url);
  }

  function viewerCategoryText(edition, paper) {
    const rank = paper.rank === null || paper.rank === undefined ? "" : ` · #${paper.rank}`;
    if (activeMode === "daily") return `${paper.daily_date}${rank}`;
    return `${formatWeek(edition)} · ${formatDaily(paper.daily_date)}${rank}`;
  }

  function openViewer(edition, paper, view = "reel", updateUrl = true) {
    activePaper = paper;
    reelReady = false;
    paperReady = false;
    viewerTitle.textContent = paper.title;
    viewerCategory.textContent = viewerCategoryText(edition, paper);
    viewerQA.href = paper.qa_url;
    pdfFallbackLink.href = paperPdfUrl(paper.pdf_url);
    reelFrame.src = paper.reel_url;
    paperFrame.src = "about:blank";
    showView(view === "paper" ? "paper" : "reel", updateUrl);
    if (!viewer.open) viewer.showModal();
    document.body.classList.add("viewer-open");
  }

  function closeViewer(updateUrl = true) {
    viewer.close();
    document.body.classList.remove("viewer-open");
    reelFrame.src = "about:blank";
    paperFrame.src = "about:blank";
    activePaper = null;
    reelReady = false;
    paperReady = false;
    if (updateUrl) writeSelectionUrl();
  }

  function restoreLocation() {
    const params = new URLSearchParams(window.location.search);
    const requestedMode = params.get("mode");
    const legacyDate = !requestedMode && params.has("date");
    const mode = requestedMode === "daily" || legacyDate ? "daily" : "weekly";
    const key = mode === "daily" ? params.get("date") : params.get("week");

    switchMode(mode, false, key);

    const paperId = params.get("paper");
    const record = paperId
      ? activeRecords.find(item => item.paper.paper_id === paperId)
      : null;
    if (record) {
      openViewer(record.edition, record.paper, params.get("view") === "paper" ? "paper" : "reel", false);
      setPermalink(params.get("view") === "paper" ? "paper" : "reel");
    } else {
      writeSelectionUrl();
    }
  }

  async function requestEditions({fresh = false} = {}) {
    const response = await fetch(EDITIONS_API_URL, {
      cache: fresh ? "no-store" : "default",
      credentials: "omit",
      headers: {"Accept": "application/json"},
      mode: "cors"
    });
    if (!response.ok) throw new Error("edition request failed");
    return ((await response.json()).editions || [])
      .filter(edition => edition && edition.week_id && String(edition.week_id) >= "2026-W34")
      .sort((left, right) => String(right.week_end).localeCompare(String(left.week_end)));
  }

  function syncOpenViewer(previousPaper, previousView) {
    if (!previousPaper || !viewer.open) return;
    const record = activeRecords.find(item => item.paper.paper_id === previousPaper.paper_id);
    if (!record) {
      closeViewer(false);
      return;
    }

    const nextPaper = record.paper;
    const reelChanged = previousPaper.reel_url !== nextPaper.reel_url;
    const pdfChanged = previousPaper.pdf_url !== nextPaper.pdf_url;
    activePaper = nextPaper;
    viewerTitle.textContent = nextPaper.title;
    viewerCategory.textContent = viewerCategoryText(record.edition, nextPaper);
    viewerQA.href = nextPaper.qa_url;
    pdfFallbackLink.href = paperPdfUrl(nextPaper.pdf_url);

    if (reelChanged) {
      reelReady = false;
      reelFrame.src = nextPaper.reel_url;
    }
    if (pdfChanged && paperFrame.getAttribute("src") !== "about:blank") {
      paperReady = false;
      paperFrame.src = paperPdfUrl(nextPaper.pdf_url);
    }
    showView(previousView, false);
  }

  async function refreshEditions() {
    const updatedEditions = await requestEditions({fresh: true});
    if (!updatedEditions.length) return false;
    const nextSignature = JSON.stringify(updatedEditions);
    if (nextSignature === editionsSignature) return false;

    if (!editionsSignature) {
      editions = updatedEditions;
      editionsSignature = nextSignature;
      restoreLocation();
      return true;
    }

    const mode = activeMode;
    const selection = activeSelection;
    const query = searchInput.value;
    const sort = sortButtons.find(button => button.getAttribute("aria-pressed") === "true")?.dataset.sort || "votes-desc";
    const preservedVisibleLimit = visiblePaperLimit;
    const previousPaper = activePaper;
    const previousView = paperTab.getAttribute("aria-selected") === "true" ? "paper" : "reel";
    editions = updatedEditions;
    editionsSignature = nextSignature;

    const values = populateSelector(mode);
    const nextSelection = values.includes(selection) ? selection : values[0];
    if (!nextSelection) return false;
    renderSelection(mode, nextSelection, false, preservedVisibleLimit);
    searchInput.value = query;
    setSortState(sort);
    applyCatalogControls();
    syncOpenViewer(previousPaper, previousView);
    if (activePaper && viewer.open) setPermalink(previousView);
    else writeSelectionUrl();
    return true;
  }

  function refreshContent() {
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = refreshEditions()
      .catch(() => false)
      .finally(() => {
        refreshInFlight = null;
      });
    return refreshInFlight;
  }

  function bindInteractions() {
    select.addEventListener("change", () => renderSelection(activeMode, select.value));
    ensurePaperStreamObserver();
    searchInput.addEventListener("input", () => applyCatalogControls({resetStream: true}));
    for (const button of sortButtons) button.addEventListener("click", handleSortClick);
    olderButton.addEventListener("click", () => stepEdition(1));
    newerButton.addEventListener("click", () => stepEdition(-1));
    for (const button of modeButtons) {
      button.addEventListener("click", () => {
        const nextMode = button.dataset.mode;
        if (nextMode === activeMode) return;
        if (nextMode === "daily") {
          const dates = dailyDatesForWeek(activeSelection);
          switchMode("daily", true, dates.includes(selectedDaily) ? selectedDaily : dates[0]);
        } else {
          switchMode("weekly", true, weekForDate(activeSelection)?.week_id || selectedWeekly);
        }
      });
    }
  }

  async function load() {
    bindInteractions();
    try {
      editions = await requestEditions();
      if (!editions.length) {
        loading.hidden = true;
        empty.hidden = false;
        updateSearchPaperCount(0);
        region.setAttribute("aria-busy", "false");
        return;
      }
      editionsSignature = JSON.stringify(editions);
      restoreLocation();
    } catch (_error) {
      loading.textContent = "Trending Paper is temporarily unavailable. Please try again shortly.";
      region.setAttribute("aria-busy", "false");
    }
  }

  document.querySelector("#viewer-back").addEventListener("click", () => closeViewer());
  viewer.addEventListener("cancel", event => { event.preventDefault(); closeViewer(); });
  reelTab.addEventListener("click", () => showView("reel"));
  paperTab.addEventListener("click", () => showView("paper"));
  reelFrame.addEventListener("load", () => {
    if (reelFrame.getAttribute("src") === "about:blank") return;
    reelReady = true;
    if (!reelPanel.hidden) viewerLoading.hidden = true;
  });
  paperFrame.addEventListener("load", () => {
    if (paperFrame.getAttribute("src") === "about:blank") return;
    paperReady = true;
    if (!paperPanel.hidden) viewerLoading.hidden = true;
  });
  window.setInterval(() => {
    if (!document.hidden) refreshContent();
  }, CONTENT_REFRESH_INTERVAL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshContent();
  });
  window.addEventListener("online", refreshContent);
  loadGitHubStars();
  load();
})();
