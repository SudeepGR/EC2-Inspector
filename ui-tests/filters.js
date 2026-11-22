const fs = require("fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const HTML = fs.readFileSync(require("path").join(__dirname, "..", "app", "static", "index.html"), "utf8");

const inst = (id, name, state, platform, ssm, tags, verdict) => ({
  instance_id: id, name, state, instance_type: "t3.micro", availability_zone: "us-east-1a", platform,
  private_ip: "10.0.0.1", public_ip: null, uptime_hours: state === "running" ? 5 : null, tags,
  health: { verdict, reasons: [] },
  ssm_agent: ssm === "online" ? { registered: true, ping_status: "Online" }
    : ssm === "notonline" ? { registered: true, ping_status: "ConnectionLost" }
    : { registered: false, ping_status: null },
});
const FLEET = [
  inst("i-001", "web-1", "running", "Linux", "online", { Environment: "prod", Owner: "a" }, "HEALTHY"),
  inst("i-002", "web-2", "running", "Linux", "notonline", { Environment: "dev" }, "WARNING"),
  inst("i-003", "win-1", "running", "Windows", "none", { Environment: "prod" }, "WARNING"),
  inst("i-004", "win-2", "stopped", "Windows", "none", {}, "STOPPED"),
  inst("i-005", "db-1", "running", "Linux", "online", { Owner: "b" }, "HEALTHY"),
];
const payload = {
  region: "us-east-1", generated_at: new Date().toISOString(), data_sources: { ec2: "ok", ssm: "ok" },
  totals: { instances: 5, by_verdict: { HEALTHY: 2, WARNING: 2, STOPPED: 1 }, by_state: {} }, instances: FLEET,
};

let failures = 0;
const check = (label, actual, expected) => {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${ok ? "" : `  (got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)})`}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function open(search = "") {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(HTML, {
    url: "http://localhost:8000/" + search, runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(window) {
      window.fetch = async (url) => {
        const body = String(url).startsWith("/api/regions")
          ? { default: "us-east-1", regions: ["us-east-1", "eu-west-1"] } : payload;
        return { ok: true, status: 200, json: async () => body };
      };
    },
  });
  await sleep(400);
  const d = dom.window.document;
  const api = {
    dom, d, errors,
    names: () => [...d.querySelectorAll("#rows tr td:first-child")].map((td) => td.textContent),
    set(id, value) { const el = d.getElementById(id); el.value = value; el.dispatchEvent(new dom.window.Event(id === "search" ? "input" : "change")); },
    options: (id) => [...d.querySelectorAll(`#${id} option`)].map((o) => o.value),
    click: (id) => d.getElementById(id).click(),
    query: () => dom.window.location.search,
  };
  return api;
}

(async () => {
  let ui = await open();
  check("loads all 5 instances", ui.names().length, 5);

  ui.set("f-state", "stopped");
  check("state=stopped", ui.names(), ["win-2"]);
  ui.click("clear");

  ui.set("f-platform", "Windows");
  check("platform=Windows", ui.names().sort(), ["win-1", "win-2"]);
  ui.click("clear");

  ui.set("f-ssm", "online");
  check("ssm=online", ui.names().sort(), ["db-1", "web-1"]);
  ui.set("f-ssm", "notonline");
  check("ssm=registered but not online", ui.names(), ["web-2"]);
  ui.set("f-ssm", "notregistered");
  check("ssm=not registered (running only; stopped is n/a)", ui.names(), ["win-1"]);
  ui.click("clear");

  check("tag key options come from the data", ui.options("f-tagkey"), ["", "Environment", "Owner"]);
  check("tag value select disabled until a key is chosen", ui.d.getElementById("f-tagval").disabled, true);
  ui.set("f-tagkey", "Environment");
  check("tag value options for Environment", ui.options("f-tagval"), ["", "__NOT_SET__", "dev", "prod"]);
  check("tag key only (has the tag)", ui.names().sort(), ["web-1", "web-2", "win-1"]);
  ui.set("f-tagval", "prod");
  check("Environment=prod", ui.names().sort(), ["web-1", "win-1"]);
  ui.set("f-tagval", "__NOT_SET__");
  check("Environment (not set)", ui.names().sort(), ["db-1", "win-2"]);
  ui.set("f-tagval", "prod");

  ui.set("f-platform", "Windows");
  check("Windows AND Environment=prod", ui.names(), ["win-1"]);
  check("filters are written to the URL", ["region=us-east-1", "platform=Windows", "tagKey=Environment", "tagValue=prod"].every((p) => ui.query().includes(p)), true);

  ui.click("clear");
  check("clear restores every row", ui.names().length, 5);
  check("clear resets all controls",
    ["f-state", "f-platform", "f-ssm", "f-tagkey", "f-tagval"].map((id) => ui.d.getElementById(id).value), ["", "", "", "", ""]);
  check("clear removes filters from the URL", ui.query(), "?region=us-east-1");

  ui.set("search", "win");
  check("search 'win'", ui.names().sort(), ["win-1", "win-2"]);
  ui.set("f-state", "stopped");
  check("search AND state", ui.names(), ["win-2"]);
  ui.click("clear");

  const cards = [...ui.d.querySelectorAll(".card")];
  cards.find((c) => c.textContent.includes("Warning")).click();
  check("verdict card still filters", ui.names().sort(), ["web-2", "win-1"]);
  ui.set("f-platform", "Linux");
  check("verdict card AND platform", ui.names(), ["web-2"]);
  check("no JS errors during interaction", ui.errors, []);
  ui.dom.window.close();

  // Opening a shared link restores the filters.
  ui = await open("?state=running&platform=Linux&tagKey=Owner&tagValue=b");
  check("link restores filters", ui.names(), ["db-1"]);
  check("link restores control values",
    ["f-state", "f-platform", "f-tagkey", "f-tagval"].map((id) => ui.d.getElementById(id).value), ["running", "Linux", "Owner", "b"]);
  ui.dom.window.close();

  ui = await open("?state=bogus&platform=Mac&ssm=x&verdict=NOPE&tagKey=DoesNotExist&tagValue=zzz");
  check("invalid or unknown link values are ignored", ui.names().length, 5);
  check("unknown tag key from link is reset", ui.d.getElementById("f-tagkey").value, "");
  check("no JS errors with bad link", ui.errors, []);
  ui.dom.window.close();

  ui = await open("?q=%3Cscript%3Ealert(1)%3C%2Fscript%3E");
  check("hostile search text is inert", [ui.d.querySelectorAll("#rows script").length, ui.d.getElementById("search").value], [0, "<script>alert(1)</script>"]);
  ui.dom.window.close();

  console.log(failures ? `\n${failures} FAILED` : "\nAll filter UI checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error("TEST CRASH", e); process.exit(1); });
