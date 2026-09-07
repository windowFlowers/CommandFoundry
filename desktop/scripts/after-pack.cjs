const fs = require("node:fs");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);

module.exports = async function stampWindowsExecutable(context) {
  if (context.electronPlatformName !== "win32") {
    return;
  }

  const desktopRoot = path.resolve(__dirname, "..");
  const executable = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.exe`);
  const icon = path.join(desktopRoot, "assets", "icon.ico");
  const rcedit = path.join(
    desktopRoot,
    "node_modules",
    "electron-winstaller",
    "vendor",
    "rcedit.exe",
  );
  for (const requiredPath of [executable, icon, rcedit]) {
    if (!fs.existsSync(requiredPath)) {
      throw new Error(`Windows resource stamping dependency not found: ${requiredPath}`);
    }
  }

  const version = context.packager.appInfo.version;
  const numericVersion = `${version}.0`;
  await execFileAsync(rcedit, [
    executable,
    "--set-icon",
    icon,
    "--set-file-version",
    numericVersion,
    "--set-product-version",
    numericVersion,
    "--set-version-string",
    "ProductName",
    "AegisCopilot",
    "--set-version-string",
    "FileDescription",
    "AegisCopilot",
    "--set-version-string",
    "CompanyName",
    "AegisCopilot Contributors",
    "--set-version-string",
    "OriginalFilename",
    "AegisCopilot.exe",
    "--set-version-string",
    "ProductVersion",
    version,
  ]);
};
