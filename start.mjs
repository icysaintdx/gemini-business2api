#!/usr/bin/env node
import { execSync, spawn } from 'child_process';
import { existsSync } from 'fs';
import { createInterface } from 'readline';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = __dirname;

// ── 颜色 ──────────────────────────────────────────────────────────────────
const C = {
  reset:  '\x1b[0m',
  green:  '\x1b[32m',
  red:    '\x1b[31m',
  yellow: '\x1b[33m',
  blue:   '\x1b[34m',
  cyan:   '\x1b[36m',
  bold:   '\x1b[1m',
};
const ok   = msg => console.log(`${C.green}✓ ${msg}${C.reset}`);
const err  = msg => console.log(`${C.red}✗ ${msg}${C.reset}`);
const info = msg => console.log(`${C.yellow}→ ${msg}${C.reset}`);
const step = msg => console.log(`\n${C.blue}${C.bold}[STEP] ${msg}${C.reset}`);

// ── 工具函数 ──────────────────────────────────────────────────────────────
function run(cmd, opts = {}) {
  execSync(cmd, { stdio: 'inherit', cwd: ROOT, ...opts });
}

function which(bin) {
  try {
    execSync(`which ${bin}`, { stdio: 'ignore' });
    return true;
  } catch {
    try {
      execSync(`where ${bin}`, { stdio: 'ignore' });
      return true;
    } catch {
      return false;
    }
  }
}

// 根据平台返回 venv 内的 python / pip 路径
function venvPython() {
  const win = process.platform === 'win32';
  return win
    ? resolve(ROOT, '.venv', 'Scripts', 'python.exe')
    : resolve(ROOT, '.venv', 'bin', 'python');
}

// ── 核心步骤 ──────────────────────────────────────────────────────────────
function ensureVenv() {
  step('创建虚拟环境 .venv');
  if (existsSync(resolve(ROOT, '.venv'))) {
    info('.venv 已存在，跳过创建');
    return;
  }

  if (which('uv')) {
    info('使用 uv 创建 .venv（Python 3.11）...');
    run('uv venv --python 3.11 .venv');
  } else if (which('python3')) {
    info('使用 python3 -m venv 创建 .venv...');
    run('python3 -m venv .venv');
  } else if (which('python')) {
    info('使用 python -m venv 创建 .venv...');
    run('python -m venv .venv');
  } else {
    err('未找到 Python，请先安装 Python 3.11+');
    process.exit(1);
  }
  ok('.venv 创建成功');
}

function installDeps() {
  step('安装 Python 依赖');
  const python = venvPython();

  if (!existsSync(python)) {
    err('.venv 不存在，请先执行"初始化环境"');
    process.exit(1);
  }

  if (which('uv')) {
    info('使用 uv 安装依赖...');
    run(`uv pip install --python "${python}" -r requirements.txt`);
  } else {
    info('使用 pip 安装依赖...');
    run(`"${python}" -m pip install --upgrade pip`);
    run(`"${python}" -m pip install -r requirements.txt`);
  }
  ok('依赖安装完成');
}

function buildFrontend() {
  step('安装并构建前端');
  const frontendDir = resolve(ROOT, 'frontend');

  if (!existsSync(frontendDir)) {
    err('frontend 目录不存在');
    process.exit(1);
  }

  if (!which('npm')) {
    err('未找到 npm，请先安装 Node.js');
    process.exit(1);
  }

  info('npm install ...');
  run('npm install', { cwd: frontendDir });

  info('npm run build ...');
  run('npm run build', { cwd: frontendDir });

  ok('前端构建完成');
}

function startServer() {
  step('启动服务');
  const python = venvPython();

  if (!existsSync(python)) {
    err('.venv 不存在，请先执行"初始化环境"');
    process.exit(1);
  }

  info(`使用 ${python} 启动 main.py ...`);
  console.log(`${C.cyan}${'─'.repeat(50)}${C.reset}`);

  const child = spawn(python, ['main.py'], {
    cwd: ROOT,
    stdio: 'inherit',
    env: { ...process.env },
  });

  child.on('exit', code => {
    if (code !== 0) err(`服务退出，退出码：${code}`);
  });

  // 透传信号
  ['SIGINT', 'SIGTERM'].forEach(sig =>
    process.on(sig, () => child.kill(sig))
  );
}

// ── 菜单 ──────────────────────────────────────────────────────────────────
function printMenu() {
  console.log(`
${C.bold}${C.cyan}╔══════════════════════════════════════╗
║   Gemini Business2API  启动脚本      ║
╚══════════════════════════════════════╝${C.reset}

  ${C.green}1${C.reset}  一键启动        （初始化 + 安装依赖 + 构建前端 + 启动服务）
  ${C.green}2${C.reset}  初始化环境      （创建 .venv + 安装 Python 依赖 + 构建前端）
  ${C.green}3${C.reset}  仅启动服务      （跳过初始化，直接启动）
  ${C.green}4${C.reset}  重新安装依赖    （重装 Python 依赖 + 重建前端）
  ${C.green}5${C.reset}  仅重建前端      （npm install + npm run build）
  ${C.green}0${C.reset}  退出
`);
}

async function prompt(question) {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  return new Promise(resolve =>
    rl.question(question, ans => { rl.close(); resolve(ans.trim()); })
  );
}

async function main() {
  printMenu();
  const choice = await prompt(`${C.bold}请选择操作 [0-5]: ${C.reset}`);

  switch (choice) {
    case '1':
      ensureVenv();
      installDeps();
      buildFrontend();
      startServer();
      break;
    case '2':
      ensureVenv();
      installDeps();
      buildFrontend();
      ok('环境初始化完成，可运行 "node start.mjs" 选择 3 启动服务');
      break;
    case '3':
      startServer();
      break;
    case '4':
      installDeps();
      buildFrontend();
      break;
    case '5':
      buildFrontend();
      break;
    case '0':
      info('已退出');
      process.exit(0);
      break;
    default:
      err(`无效选项：${choice}`);
      process.exit(1);
  }
}

main().catch(e => { err(e.message); process.exit(1); });
