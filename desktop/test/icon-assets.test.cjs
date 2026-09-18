const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const assetsDir = path.resolve(__dirname, "..", "assets");

test("ImageGen application icon includes PNG source and all Windows sizes", () => {
  const png = fs.readFileSync(path.join(assetsDir, "icon.png"));
  assert.deepEqual([...png.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
  const ico = fs.readFileSync(path.join(assetsDir, "icon.ico"));
  assert.equal(ico.readUInt16LE(0), 0);
  assert.equal(ico.readUInt16LE(2), 1);
  assert.equal(ico.readUInt16LE(4), 6);
  const sizes = [];
  for (let index = 0; index < 6; index += 1) {
    const offset = 6 + index * 16;
    sizes.push(ico[offset] || 256);
  }
  assert.deepEqual(sizes, [16, 32, 48, 64, 128, 256]);
});

test("Windows packaging stamps AegisCopilot metadata and recreates shortcuts", () => {
  const desktopRoot = path.resolve(__dirname, "..");
  const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));
  const mainSource = fs.readFileSync(path.join(desktopRoot, "electron", "main.cjs"), "utf8");
  const backendBuild = fs.readFileSync(path.join(desktopRoot, "scripts", "build-backend.ps1"), "utf8");
  const afterPackPath = path.join(desktopRoot, "scripts", "after-pack.cjs");

  assert.equal(packageJson.version, "2.10.0");
  assert.equal(packageJson.build.win.signAndEditExecutable, false);
  assert.equal(packageJson.build.afterPack, "scripts/after-pack.cjs");
  assert.equal(fs.existsSync(afterPackPath), true);
  const afterPackSource = fs.readFileSync(afterPackPath, "utf8");
  assert.match(afterPackSource, /rcedit\.exe/);
  assert.match(afterPackSource, /--set-icon/);
  assert.match(afterPackSource, /ProductName/);
  assert.equal(packageJson.build.nsis.createDesktopShortcut, "always");
  assert.equal(packageJson.build.nsis.shortcutName, "AegisCopilot");
  assert.match(mainSource, /app\.setAppUserModelId\("com\.aegiscopilot\.desktop"\)/);
  assert.match(backendBuild, /collect-all fastembed/);
  assert.match(mainSource, /titleBarStyle: "hidden"/);
  assert.match(mainSource, /Menu\.setApplicationMenu\(null\)/);
});
