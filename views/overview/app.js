// 社区雷达状态页：只读，全部数据来自插件注册的 /overview 接口。
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);

function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function card(key, value, hint, small) {
  const box = document.createElement("div");
  box.className = "card";
  const k = document.createElement("div");
  k.className = "k";
  k.textContent = key;
  const v = document.createElement("div");
  v.className = small ? "v small" : "v";
  v.textContent = value;
  box.append(k, v);
  if (hint) {
    const h = document.createElement("div");
    h.className = "hint";
    h.textContent = hint;
    box.appendChild(h);
  }
  return box;
}

function line(children) {
  const row = document.createElement("div");
  row.className = "row";
  for (const child of [].concat(children)) {
    if (typeof child === "string") {
      const span = document.createElement("span");
      span.textContent = child;
      row.appendChild(span);
    } else {
      row.appendChild(child);
    }
  }
  return row;
}

function tag(text, cls) {
  const span = document.createElement("span");
  span.className = cls ? `tag ${cls}` : "tag";
  span.textContent = text;
  return span;
}

function render(data) {
  $("meta").textContent = `更新于 ${when(data.generated_at)} · 下次浏览 ${data.browse.next_run}`;

  const cards = $("cards");
  cards.innerHTML = "";
  const share = data.share;
  const ledger = data.ledger || {};
  const knowledge = data.knowledge || {};

  cards.append(
    card("自动浏览", data.browse.enabled ? "已开启" : "已关闭",
         `平台：${data.browse.platforms}`, true),
    card("下次浏览", data.browse.next_run, "", true),
    card("今日分享", `${share.today} / ${share.max_per_day ?? "—"}`,
         share.dry_run ? "试运行中，不投递" : "真实发送"),
    card("看过记住", ledger.memory ?? 0, "条观看记忆"),
    card("知识沉淀", knowledge.total ?? 0,
         `知识 ${knowledge.knowledge ?? 0} · 梗 ${knowledge.meme ?? 0} · 名词 ${knowledge.term ?? 0}`,
         true),
    card("想刷列表", (data.watchlist || []).length, "条待看", true),
    card("画面来源", data.video.frame_source,
         `宽 ${data.video.frame_width ?? "—"} · ${data.video.frame_compose}`, true),
    card("硬字幕 OCR", data.video.ocr.enabled ? "已开启" : "未开启",
         `fps ${data.video.ocr.fps} · 最多 ${data.video.ocr.max_frames} 帧`, true),
  );

  const last = data.browse.last || {};
  const activity = data.browse.activity || {};
  const lastBox = $("last");
  lastBox.innerHTML = "";
  if (!last.at) {
    lastBox.textContent = "还没有跑过一轮。";
  } else {
    lastBox.appendChild(line([
      tag(last.shared ? "已分享" : "没有发"),
      `看懂 ${last.analyzed ?? 0} 条`,
      last.reason ? tag(last.reason, "dim") : "",
    ]));
    if (activity.stage) {
      lastBox.appendChild(line([`当前：${activity.stage}`]));
    }
    if (last.at) lastBox.appendChild(line([`时间：${when(last.at)}`, tag(`预算 ${data.share.min_score ?? "—"} 分才发`, "dim")]));
  }

  const interests = $("interests");
  interests.innerHTML = "";
  if (!(data.interests || []).length) {
    interests.textContent = "还没有积累出明显的偏好。";
  } else {
    for (const item of data.interests) {
      const tone = item.polarity === "dislike" ? "warn" : "ok";
      interests.appendChild(line([
        tag(item.polarity || "?", tone),
        item.value || "—",
        item.strength != null ? tag(String(item.strength), "dim") : "",
      ]));
    }
  }

  const watch = $("watchlist");
  watch.innerHTML = "";
  if (!(data.watchlist || []).length) {
    watch.textContent = "暂时没有想自己去刷的东西。";
  } else {
    for (const item of data.watchlist) {
      watch.appendChild(line([
        item.title || item.url || "—",
        item.from ? tag(`来自 ${item.from}`, "dim") : "",
      ]));
    }
  }

  const health = $("health");
  health.innerHTML = "";
  const entries = Object.entries(data.health || {});
  if (!entries.length) {
    health.appendChild(line([tag("各平台正常", "ok")]));
  } else {
    for (const [name, info] of entries) {
      health.appendChild(line([
        tag(name, "warn"),
        `连续失败 ${info.fails} 次`,
        info.reason ? tag(info.reason, "dim") : "",
      ]));
    }
  }
}

async function load() {
  $("meta").textContent = "正在读取…";
  try {
    const data = await bridge.apiGet("overview");
    render(data);
  } catch (err) {
    $("meta").textContent = `读取失败：${err && err.message ? err.message : err}`;
  }
}

await bridge.ready();
$("refresh").addEventListener("click", load);
await load();
