"use strict";

let dashboard = null;
let selectedDay = 0;
const byId = (id) => document.getElementById(id);
const node = (tag, className, text) => {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
};
const formatTime = (value) => value ? new Intl.DateTimeFormat("uk-UA", {
  timeZone: dashboard?.timezone || "Europe/Kyiv", day: "2-digit", month: "2-digit",
  hour: "2-digit", minute: "2-digit",
}).format(new Date(value)) : "Ще не було";
const duration = (minutes) => {
  if (!minutes) return "0 год";
  return [Math.floor(minutes / 60) ? `${Math.floor(minutes / 60)} год` : "",
    minutes % 60 ? `${minutes % 60} хв` : ""].filter(Boolean).join(" ");
};

function renderQueue(queue, prefix) {
  byId(`${prefix}-queue`).textContent = queue.queue;
  const container = byId(`${prefix}-intervals`);
  container.replaceChildren();
  if (queue.status !== "published") {
    container.append(node("p", "muted", queue.status === "emergency"
      ? "Аварійні обмеження. Опублікований план не застосовується."
      : "Графік не опубліковано. Даних про відключення немає."));
  } else {
    if (!queue.intervals.length) container.append(node("p", "muted", "Планових відключень у графіку не зазначено."));
    for (const [intervals, possible] of [[queue.intervals, false], [queue.possible, true]]) {
      if (possible && intervals.length) container.append(node("p", "possible-title", "Можливі відключення"));
      for (const interval of intervals) {
        const row = node("div", `interval-row ${interval.phase}`);
        row.append(node("span", "interval-time", `${interval.start_label}–${interval.end_label}`));
        const label = possible ? "Можливе відключення" : interval.phase === "past" ? "Минуло за графіком"
          : interval.phase === "current" ? "Інтервал триває за планом" : "Без світла за планом";
        row.append(node("span", "interval-label", label));
        container.append(row);
      }
    }
  }
  byId(`${prefix}-update`).textContent = queue.fetched_at
    ? `${queue.stale ? "Дані застаріли. " : ""}Отримано ${formatTime(queue.fetched_at)}. Оновлення джерела: ${queue.source_updated_at ? formatTime(queue.source_updated_at) : "не вказано"}.`
    : "Ще немає збережених даних для цього дня.";
}

function renderTimeline(day) {
  const container = byId("timelines");
  container.replaceChildren();
  for (const [label, suffix, intervals, possible, known] of [
    ["Квартира", day.home.queue, day.home.intervals, day.home.possible, day.home.status === "published"],
    ["Друга лінія", day.secondary.queue, day.secondary.intervals, day.secondary.possible, day.secondary.status === "published"],
    ["Ліфт", "перетин", day.lift.intervals, [], day.lift.known],
  ]) {
    const kind = label === "Квартира" ? "home" : label === "Ліфт" ? "lift" : "secondary";
    const row = node("div", "timeline-row");
    const title = node("div", "timeline-label", label);
    title.append(node("span", "", suffix));
    const track = node("div", `track${known ? "" : " unknown"}`);
    track.setAttribute("role", "img");
    track.setAttribute("aria-label", `${label}: ${known ? intervals.map(i => `${i.start_label}–${i.end_label}`).join(", ") || "без планових інтервалів" : "даних немає"}`);
    for (const [list, maybe] of [[intervals, false], [possible, true]]) {
      for (const interval of list) {
        const bar = node("span", `bar ${kind} ${interval.phase}${maybe ? " possible" : ""}`);
        bar.style.left = `${interval.left}%`;
        bar.style.width = `${interval.width}%`;
        bar.title = `${interval.start_label}–${interval.end_label}${maybe ? " · можливе відключення" : ""}`;
        track.append(bar);
      }
    }
    if (day.now_position !== null) {
      const marker = node("span", "now-marker");
      marker.style.left = `${day.now_position}%`;
      marker.title = "Поточний час";
      track.append(marker);
    }
    row.append(title, track);
    container.append(row);
  }
  const axis = node("div", "axis");
  for (const tick of day.ticks) {
    const label = node("span", "", tick.label);
    label.style.left = `${tick.left}%`;
    axis.append(label);
  }
  container.append(axis);
}

function renderLift(day) {
  const total = day.lift.intervals.reduce((sum, interval) => sum + interval.minutes, 0);
  const current = day.lift.intervals.find(i => i.phase === "current");
  const upcoming = day.lift.intervals.find(i => i.phase === "upcoming");
  const card = document.querySelector(".lift-card");
  byId("lift-total").textContent = day.lift.known ? duration(total) : "—";
  card.dataset.tone = !day.lift.known || !day.lift.fresh ? "unknown" : current ? "warning" : "normal";
  let heading, description;
  if (!day.lift.known) {
    heading = "Недостатньо даних для прогнозу";
    description = "Потрібні опубліковані графіки обох ліній на цей день.";
  } else if (!day.lift.fresh) {
    heading = "Прогноз потребує оновлення";
    description = "Показано збережені інтервали. Їхню актуальність не підтверджено.";
  } else if (current) {
    heading = "Можливий простій зараз";
    description = `Перетин відключень за графіком: ${current.start_label}–${current.end_label}.`;
  } else if (upcoming) {
    heading = selectedDay === 0 ? "Наступний можливий простій" : "Прогноз на завтра";
    description = `${upcoming.start_label}–${upcoming.end_label} · ${day.lift.intervals.length === 1 ? "єдиний інтервал" : `інтервалів за день: ${day.lift.intervals.length}`}.`;
  } else {
    heading = total ? "Нових перетинів сьогодні не заплановано" : "Перетинів у графіках немає";
    description = "Одночасних майбутніх відключень обох ліній за цим планом не передбачено.";
  }
  byId("lift-heading").textContent = heading;
  byId("lift-description").textContent = description;
}

function render() {
  const day = dashboard.days[selectedDay];
  const monitoring = dashboard.monitoring;
  byId("operator").textContent = dashboard.operator;
  byId("selected-date").textContent = day.date_label;
  const labels = {ok: "Джерело перевірено", error: "Помилка оновлення", stale: "Дані застаріли", loading: "Отримуємо графіки"};
  byId("connection").textContent = labels[monitoring.state];
  byId("connection").dataset.state = monitoring.state;
  byId("monitor-title").textContent = labels[monitoring.state];
  byId("last-check").textContent = formatTime(monitoring.last_check);
  byId("last-success").textContent = formatTime(monitoring.last_success);
  byId("check-interval").textContent = `Кожні ${dashboard.check_interval_minutes} хвилин`;
  const banner = byId("banner");
  const stale = day.home.stale || day.secondary.stale;
  banner.hidden = !monitoring.error && !stale;
  banner.textContent = monitoring.error
    ? `Джерело не вдалося оновити: ${monitoring.error}. Останні отримані дані збережено.`
    : `Дані не підтверджено протягом ${dashboard.stale_after_minutes} хвилин. Показано останній збережений графік.`;
  renderQueue(day.home, "home");
  renderQueue(day.secondary, "secondary");
  renderTimeline(day);
  renderLift(day);
}

let fetching = false;
async function refresh() {
  if (fetching) return;
  fetching = true;
  try {
    const response = await fetch("/api/dashboard", {cache: "no-store", signal: AbortSignal.timeout(15000)});
    if (!response.ok) throw new Error("Недоступний сервер");
    dashboard = await response.json();
    render();
  } catch {
    byId("connection").textContent = "Немає зв’язку з сервером";
    byId("connection").dataset.state = "error";
    byId("banner").hidden = false;
    byId("banner").textContent = "Не вдалося завантажити інформацію. Показані дані можуть бути застарілими. Спробуємо ще раз автоматично.";
  } finally {
    fetching = false;
  }
}

for (const button of document.querySelectorAll("[data-day]")) {
  button.addEventListener("click", () => {
    selectedDay = Number(button.dataset.day);
    for (const other of document.querySelectorAll("[data-day]")) other.setAttribute("aria-pressed", String(other === button));
    if (dashboard) render();
  });
}
function tickClock() {
  byId("clock").textContent = new Intl.DateTimeFormat("uk-UA", {timeZone: "Europe/Kyiv", hour: "2-digit", minute: "2-digit"}).format(new Date());
}
tickClock();
setInterval(tickClock, 1000);
refresh();
setInterval(refresh, 30000);
