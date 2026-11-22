const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const HTML = fs.readFileSync(require("path").join(__dirname, "..", "app", "static", "index.html"), "utf8");

const inst = (id, name, state, platform, ssm, tags, verdict, ip, uptime, reasons = []) => ({
  instance_id: id, name, state, instance_type: "t3.micro", availability_zone: "us-east-1a", platform,
  private_ip: ip, public_ip: null, uptime_hours: uptime, tags, volume_count: 1,
  health: { verdict, reasons },
  ssm_agent: ssm === "online" ? { registered: true, ping_status: "Online" }
    : ssm === "notonline" ? { registered: true, ping_status: "ConnectionLost" } : { registered: false, ping_status: null },
});
const W = [{ severity: "WARNING", code: "X", message: "something" }];
const FLEET = [
  inst("i-001", "web-1", "running", "Linux", "online", { Environment: "prod" }, "HEALTHY", "10.0.0.10", 50),
  inst("i-002", "web-2", "running", "Linux", "notonline", { Environment: "dev" }, "WARNING", "10.0.0.2", 5, W),
  inst("i-003", "win-1", "running", "Windows", "none", { Environment: "prod" }, "WARNING", "10.0.0.33", 500, W),
  inst("i-004", "win-2", "stopped", "Windows", "none", {}, "STOPPED", "10.0.0.4", null),
  inst("i-005", "<img src=x onerror=alert(1)>", "running", "Linux", "online", { Owner: "b" }, "HEALTHY", "10.0.0.5", 20),
];
const payload = {
  region: "us-east-1", generated_at: new Date().toISOString(), data_sources: { ec2: "ok", ssm: "ok" },
  totals: { instances: 5, by_verdict: { HEALTHY: 2, WARNING: 2, STOPPED: 1 }, by_state: {} }, instances: FLEET,
};
const detailFor = (row) => ({
  ...row, health: row.health, status_checks: { system_status: "ok", instance_status: "ok", events: [] },
  ssm: { registered: true, ping_status: "Online", last_ping: null, agent_version: "3", platform: "x" },
  image: { image_id: "ami-1", name: "img" }, architecture: "x86_64", key_name: null, iam_instance_profile: null,
  ebs_optimized: true, monitoring: "disabled", metadata_options: { http_tokens: "required" }, launch_time: null,
  network: { vpc_id: "vpc-1", subnet_id: "subnet-1", private_dns_name: null, public_dns_name: null, network_interfaces: 1, security_groups: [{ id: "sg-1", name: "web" }] },
  storage: { volumes: [] }, data_sources: { ec2: "ok", ssm: "ok", cloudwatch: "ok" },
  performance: { cpu_24h: { datapoints: 2, average: 40, maximum: 80, hourly: [{ time: new Date().toISOString(), average: 30 }, { time: new Date().toISOString(), average: 50 }] }, notes: [] },
});

let failures = 0;
const check = (label, actual, expected) => {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${ok ? "" : `  (got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)})`}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function open(search = "", { storedTheme, clipboard = "ok", execCommand } = {}) {
  const errors = [], fetched = [], copied = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(HTML, {
    url: "http://localhost:8000/" + search, runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(window) {
      if (storedTheme) window.localStorage.setItem("ec2inspector.theme", storedTheme);
      window.fetch = async (url) => {
        const u = String(url); fetched.push(u);
        const match = u.match(/^\/api\/instances\/([^?]+)/);
        const body = u.startsWith("/api/regions") ? { default: "us-east-1", regions: ["us-east-1"] }
          : match ? detailFor(FLEET.find((r) => r.instance_id === decodeURIComponent(match[1]))) : payload;
        return { ok: true, status: 200, json: async () => body };
      };
      Object.defineProperty(window.navigator, "clipboard", { value: { writeText: async (t) => {
        if (clipboard === "reject") throw new Error("NotAllowedError");
        copied.push(t);
      } } });
      if (execCommand !== undefined) window.document.execCommand = execCommand;
    },
  });
  await sleep(1200);  // let load + the count-up animation finish
  const w = dom.window, d = w.document;
  const api = {
    dom, d, w, errors, fetched, copied,
    names: () => [...d.querySelectorAll("#rows tr td:first-child")].map((td) => td.textContent),
    set(id, value) { const el = d.getElementById(id); el.value = value; el.dispatchEvent(new w.Event(id === "search" ? "input" : "change")); },
    key(k, target = d) { target.dispatchEvent(new w.KeyboardEvent("keydown", { key: k, bubbles: true })); },
    th: (label) => [...d.querySelectorAll("#head th")].find((t) => t.textContent.trim().startsWith(label)),
    sortBy(label) { api.th(label).querySelector("button").click(); },
    sortState: () => [...d.querySelectorAll("#head th")].filter((t) => t.getAttribute("aria-sort") !== "none").map((t) => `${t.textContent.trim()}:${t.getAttribute("aria-sort")}`),
    chips: () => [...d.querySelectorAll("#chips .fchip")].map((c) => c.textContent.replace(/\s+/g, " ").trim()),
    theme: () => d.documentElement.getAttribute("data-theme"),
    toasts: () => [...d.querySelectorAll("#toasts .toast")].map((t) => t.textContent),
    drawerOpen: () => d.getElementById("drawer").classList.contains("open"),
    drawerTitle: () => (d.querySelector("#drawer h2") || {}).textContent,
    seg: (label) => [...d.querySelectorAll("#ring .seg")].find((s) => s.getAttribute("aria-label").startsWith(label)),
  };
  return api;
}

(async () => {
  let ui = await open();

  // ------------------------------ sorting ------------------------------
  const original = ui.names();
  check("starts in the server order (worst first)", ui.sortState(), []);
  ui.sortBy("Name");
  check("sort Name ascending", ui.names(), [...original].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" })));
  check("header shows ascending", ui.sortState(), ["Name\u25B2:ascending"]);
  ui.sortBy("Name");
  check("second click sorts descending", ui.names()[0], "win-2");
  ui.sortBy("Name");
  check("third click returns to the server order", ui.names(), original);
  check("no header is marked sorted again", ui.sortState(), []);

  ui.sortBy("Private IP");
  check("IPs sort naturally (.2 before .10)", ui.names(), ["web-2", "win-2", "<img src=x onerror=alert(1)>", "web-1", "win-1"]);
  ui.sortBy("Uptime");
  check("uptime ascending puts empty (stopped) last", ui.names(), ["web-2", "<img src=x onerror=alert(1)>", "web-1", "win-1", "win-2"]);
  ui.sortBy("Uptime");
  check("uptime descending still puts empty last", ui.names(), ["win-1", "web-1", "<img src=x onerror=alert(1)>", "web-2", "win-2"]);
  check("sort is written to the URL", ui.w.location.search.includes("sort=uptime_hours%3Adesc"), true);
  ui.sortBy("Health");
  check("sort by Health ascending = worst first", ui.names().slice(-1), ["win-2"]);
  check("sorting did not lose any rows", ui.names().length, 5);

  // ------------------------------ ring ------------------------------
  ui.sortBy("Health"); ui.sortBy("Health");  // back to default order
  ui.seg("WARNING").dispatchEvent(new ui.w.MouseEvent("click", { bubbles: true }));
  check("clicking the WARNING ring slice filters", ui.names().sort(), ["web-2", "win-1"]);
  check("ring marks that a slice is picked", ui.d.getElementById("ring").classList.contains("picked"), true);
  check("a chip shows the health filter", ui.chips(), ["Health: WARNING\u00d7"]);
  ui.seg("WARNING").dispatchEvent(new ui.w.MouseEvent("click", { bubbles: true }));
  check("clicking the same slice again clears it", ui.names().length, 5);
  ui.key("Enter", ui.seg("STOPPED"));
  check("keyboard Enter on a slice filters", ui.names(), ["win-2"]);
  ui.seg("STOPPED").dispatchEvent(new ui.w.MouseEvent("mouseenter"));
  check("hovering a slice shows its count in the centre", [...ui.d.querySelectorAll("#ring text")].map((t) => t.textContent).slice(0, 2), ["1", "STOPPED"]);
  ui.seg("STOPPED").dispatchEvent(new ui.w.MouseEvent("mouseleave"));
  check("leaving restores the total", ui.d.querySelector("#ring .ring-n").textContent, "5");
  ui.d.getElementById("clear").click();

  // ------------------------------ chips ------------------------------
  ui.set("f-platform", "Windows"); ui.set("f-state", "running");
  check("each filter gets a chip", ui.chips(), ["State: running\u00d7", "Platform: Windows\u00d7"]);
  check("filters narrowed the table", ui.names(), ["win-1"]);
  ui.d.querySelector("#chips .fchip button").click();  // remove State
  check("removing a chip removes that filter", [ui.chips(), ui.d.getElementById("f-state").value], [["Platform: Windows\u00d7"], ""]);
  check("table updated after chip removal", ui.names().sort(), ["win-1", "win-2"]);
  ui.set("f-tagkey", "Environment"); ui.set("f-tagval", "prod");
  check("tag chip reads naturally", ui.chips().includes("Tag: Environment = prod\u00d7"), true);
  ui.set("search", "<script>alert(1)</script>");
  check("hostile search text is shown as plain text in its chip", [ui.d.querySelectorAll("#chips script").length, ui.chips().some((c) => c.includes("<script>alert(1)</script>"))], [0, true]);
  ui.d.getElementById("clear").click();
  check("Clear filters hides the chip bar", [ui.d.getElementById("chips").hidden, ui.names().length], [true, 5]);

  // ------------------------------ keyboard ------------------------------
  ui.key("/");
  check("/ focuses the search box", ui.d.activeElement.id, "search");
  ui.key("Escape", ui.d.getElementById("search"));
  check("Esc leaves the search box", ui.d.activeElement.id === "search", false);
  ui.key("?");
  check("? opens the help window", ui.d.getElementById("helpdlg").hidden, false);
  ui.key("Escape");
  check("Esc closes the help window", ui.d.getElementById("helpdlg").hidden, true);
  ui.key("j");
  check("J focuses the first row", ui.d.activeElement.getAttribute("data-id"), "i-001");
  ui.key("j", ui.d.activeElement);
  check("J again moves down one row", ui.d.activeElement.getAttribute("data-id"), "i-002");
  ui.key("k", ui.d.activeElement);
  check("K moves back up", ui.d.activeElement.getAttribute("data-id"), "i-001");
  const before = ui.fetched.length;
  ui.key("r"); await sleep(150);
  check("R refreshes (a new list request is made)", ui.fetched.length > before, true);
  check("R announces the refresh", ui.toasts().some((t) => t.startsWith("Updated: 5 instances")), true);
  ui.set("f-state", "stopped"); ui.key("c");
  check("C clears all filters", [ui.chips().length, ui.names().length], [0, 5]);
  const startTheme = ui.theme();
  ui.key("t");
  check("T switches the theme", ui.theme() !== startTheme, true);
  check("the choice is remembered", ui.w.localStorage.getItem("ec2inspector.theme"), ui.theme());
  ui.key("t");
  check("T switches back", ui.theme(), startTheme);
  ui.key("?", ui.d.getElementById("search"));
  check("shortcuts are ignored while typing", ui.d.getElementById("helpdlg").hidden, true);

  // ------------------------------ copy ------------------------------
  const copyBtn = ui.d.querySelector('#rows tr[data-id="i-001"] button.copy');
  copyBtn.click(); await sleep(100);
  check("copy button copies the instance ID", ui.copied, ["i-001"]);
  check("copying does not open the detail panel", ui.drawerOpen(), false);
  check("a toast confirms the copy", ui.toasts().includes("Instance ID copied"), true);

  // ------------------------------ detail panel ------------------------------
  const rowsInOrder = ui.d.querySelectorAll("#rows tr");
  rowsInOrder[0].click(); await sleep(200);
  check("clicking a row opens the panel", [ui.drawerOpen(), ui.drawerTitle()], [true, "web-1"]);
  check("the open row is highlighted", ui.d.querySelectorAll("#rows tr.selected").length, 1);
  check("position is shown", ui.d.querySelector("#drawer .dtools .meta").textContent, "1 of 5");
  check("Prev is disabled on the first server", ui.d.querySelector('#drawer button[aria-label="Previous server"]').disabled, true);
  ui.d.querySelector('#drawer button[aria-label="Next server"]').click(); await sleep(200);
  check("Next shows the following server", ui.drawerTitle(), "web-2");
  ui.key("ArrowRight"); await sleep(200);
  check("the right arrow key also moves on", ui.drawerTitle(), "win-1");
  ui.key("ArrowLeft"); await sleep(200);
  check("the left arrow key moves back", ui.drawerTitle(), "web-2");
  check("position updates", ui.d.querySelector("#drawer .dtools .meta").textContent, "2 of 5");
  const h3 = ui.d.querySelector("#drawer .section h3");
  h3.click();
  check("clicking a heading collapses its section", [h3.parentElement.classList.contains("collapsed"), h3.getAttribute("aria-expanded")], [true, "false"]);
  h3.click();
  check("clicking again expands it", h3.parentElement.classList.contains("collapsed"), false);
  ui.copied.length = 0;
  [...ui.d.querySelectorAll("#drawer button")].find((b) => b.textContent === "Copy JSON").click(); await sleep(100);
  check("Copy JSON copies valid JSON for the open server", JSON.parse(ui.copied[0]).instance_id, "i-002");
  ui.key("Escape");
  check("Esc closes the panel and clears the highlight", [ui.drawerOpen(), ui.d.querySelectorAll("#rows tr.selected").length], [false, 0]);

  // panel navigation follows the filtered, sorted list
  ui.set("f-platform", "Windows");
  ui.d.querySelectorAll("#rows tr")[0].click(); await sleep(200);
  check("with a filter on, the panel only walks the visible servers", ui.d.querySelector("#drawer .dtools .meta").textContent, "1 of 2");
  ui.key("Escape"); ui.d.getElementById("clear").click();

  // ------------------------------ misc ------------------------------
  check("cards end on the true counts after the count-up", [...ui.d.querySelectorAll(".card .n")].map((n) => n.textContent), ["5", "0", "2", "2", "1"]);
  ui.d.getElementById("auto").checked = true; ui.d.getElementById("auto").dispatchEvent(new ui.w.Event("change"));
  check("auto-refresh shows a countdown", ui.d.getElementById("countdown").textContent, "next refresh 60s");
  ui.d.getElementById("auto").checked = false; ui.d.getElementById("auto").dispatchEvent(new ui.w.Event("change"));
  check("turning it off clears the countdown", ui.d.getElementById("countdown").textContent, "");
  check("no JS errors during the whole session", ui.errors, []);
  ui.dom.window.close();

  // ------------------------------ copy fallbacks ------------------------------
  ui = await open("", { clipboard: "reject", execCommand: () => true });
  ui.d.querySelector('#rows tr[data-id="i-001"] button.copy').click(); await sleep(150);
  check("if the clipboard API is blocked, the fallback copy still works", ui.toasts(), ["Instance ID copied"]);
  ui.dom.window.close();
  ui = await open("", { clipboard: "reject", execCommand: () => false });
  ui.d.querySelector('#rows tr[data-id="i-001"] button.copy').click(); await sleep(150);
  check("if nothing can copy, the user is told so", [ui.toasts(), ui.d.querySelector(".toast.err") !== null], [["Copy is not available in this browser"], true]);
  check("and no leftover hidden text box remains", ui.d.querySelectorAll("textarea").length, 0);
  ui.dom.window.close();

  // ------------------------------ links, theme ------------------------------
  ui = await open("?sort=name:desc&verdict=WARNING");
  check("a link restores sort and filter together", [ui.names(), ui.sortState()], [["win-1", "web-2"], ["Name\u25BC:descending"]]);
  ui.dom.window.close();
  ui = await open("?sort=bogus:asc&verdict=NOPE");
  check("invalid sort and verdict in a link are ignored", [ui.names().length, ui.sortState()], [5, []]);
  ui.dom.window.close();
  ui = await open("", { storedTheme: "light" });
  check("a remembered theme is applied on load", ui.theme(), "light");
  check("and the button offers the other theme", ui.d.getElementById("theme").textContent, "Dark");
  ui.dom.window.close();
  ui = await open("", { storedTheme: "<script>" });
  check("a corrupted stored theme falls back to dark", ui.theme(), "dark");
  check("no JS errors with stored values", ui.errors, []);
  ui.dom.window.close();

  console.log(failures ? `\n${failures} FAILED` : "\nAll interactive UI checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error("TEST CRASH", e); process.exit(1); });
