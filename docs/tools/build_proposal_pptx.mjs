// Create an editable PowerPoint from the maintained 12-page Chinese source.
// Runtime dependencies come from the installed Codex primary runtime.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const runtimeModules = process.env.RUNTIME_NODE_MODULES;
const skillDir = process.env.PRESENTATION_SKILL_DIR;
const runtimePython = process.env.RUNTIME_PYTHON;
const revision = process.argv.find(arg => arg.startsWith('--revision='))?.slice('--revision='.length) || 'v1';
if (!/^[A-Za-z0-9]{1,16}$/.test(revision)) throw new Error('Use a safe presentation revision.');
if (!runtimeModules || !skillDir || !runtimePython) {
    throw new Error('Use the installed presentation runtime and skill paths.');
}
const requireRuntime = createRequire(path.join(runtimeModules, '__runtime__.cjs'));
const { Presentation, PresentationFile } = await import(pathToFileURL(requireRuntime.resolve('@oai/artifact-tool')).href);
const { makeNativeBulletParagraphs, finalizePresentation } = await import(pathToFileURL(path.join(skillDir, 'container_tools/artifact_tool_utils.mjs')).href);
const { GlobalFonts } = requireRuntime('@napi-rs/canvas');
const fonts = GlobalFonts.families.map(item => item.family);
const fontFamily = ['Microsoft YaHei', 'Noto Sans SC', 'SimHei'].find(font => fonts.includes(font));
if (!fontFamily) throw new Error('No verified Chinese font family is installed in the supplied runtime.');

const sourcePath = path.join(repo, 'docs/proposal/开题PPT逐页内容与演讲稿.md');
const source = await fs.readFile(sourcePath, 'utf8');
const build = path.join(repo, '.runtime/proposal-pptx-' + revision);
const draftPath = path.join(build, 'draft.pptx');
const finalPath = path.join(repo, 'docs/deliverables/开题报告-RK3568' + (revision === 'v1' ? '' : '-' + revision) + '.pptx');
for (const target of [draftPath, finalPath]) {
    if (await fs.stat(target).then(() => true).catch(() => false)) throw new Error('Refusing existing presentation: ' + target);
}
await fs.mkdir(path.join(build, 'previews'), { recursive: true });
await fs.mkdir(path.dirname(finalPath), { recursive: true });

const sections = source.split(/\n## 第\d+页 /).slice(1, 13);
if (sections.length !== 12) throw new Error('The maintained source must have 12 slides.');
const clean = value => value.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/\*\*|`/g, '').trim();
const authored = sections.map((section, index) => {
    const title = section.slice(0, section.indexOf('\n')).trim();
    const body = section.split('**页面正文**')[1]?.split(/\*\*(?:讲稿|建议视觉|可编辑架构图内容)/)[0].trim();
    const notes = section.split(/\*\*讲稿[^*]*\*\*/)[1]?.split('**依据**')[0].trim();
    const sources = section.split('**依据**')[1]?.split('\n\n')[0].trim();
    if (!body || !notes) throw new Error('Missing authored body or notes on slide ' + (index + 1));
    return { title, body, notes, sources };
});
const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const ink = '#173951', bodyInk = '#243644', paper = '#FCFCFA', muted = '#596E7A';

function text(slide, name, value, left, top, width, height, size = 28, bold = false, color = bodyInk) {
    const shape = slide.shapes.add({ geometry: 'textbox', name,
        position: { left, top, width, height }, fill: 'none',
        line: { fill: 'none', width: 0, style: 'solid' } });
    shape.text = value;
    shape.text.style = { typeface: fontFamily, fontSize: size, bold, color,
        autoFit: 'none', verticalAlignment: 'top' };
    return shape;
}

function bullets(slide, name, items, left, top, width, height, size = 28) {
    const paragraphs = makeNativeBulletParagraphs(items.map(clean), {
        marginLeftPoints: 20, hangingPoints: 10, spaceAfterPoints: 12,
    });
    return text(slide, name, paragraphs, left, top, width, height, size);
}

async function image(slide, file, left, top, width, height, alt) {
    const bytes = await fs.readFile(file);
    slide.images.add({ blob: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
        contentType: file.endsWith('.png') ? 'image/png' : 'image/jpeg', alt, fit: 'contain',
        position: { left, top, width, height } });
}

function markdownTable(body) {
    const lines = body.split('\n');
    const first = lines.findIndex(line => line.startsWith('|'));
    if (first < 0) return null;
    let end = first;
    while (lines[end]?.startsWith('|')) end++;
    return { rows: lines.slice(first, end).filter(line => !/^\|[\s:|\-]+\|\s*$/.test(line))
        .map(line => line.trim().slice(1, -1).split('|').map(clean)),
        remainder: lines.slice(end).join('\n').trim() };
}

function table(slide, rows, rowHeight = 66) {
    const columns = rows[0].length;
    const widths = columns === 3 ? [360, 350, 420] : columns === 4 ? [465, 205, 230, 230] : [490, 640];
    const native = slide.tables.add({ rows: rows.length, columns, left: 75, top: 156,
        width: 1130, height: rows.length * rowHeight, columnWidths: widths, values: rows });
    native.styleOptions = { headerRow: true, bandedRows: false };
    native.borders.assign({ style: 'solid', fill: '#D6DEE3', width: 1 });
    for (let row = 0; row < rows.length; row++) {
        native.rows[row].height = rowHeight;
        for (let column = 0; column < columns; column++) {
            const cell = native.getCell(row, column);
            cell.fill = row === 0 ? ink : row % 2 ? '#FFFFFF' : '#F1F5F7';
            cell.text.style = { typeface: fontFamily, fontSize: 24, bold: row === 0,
                color: row === 0 ? '#FFFFFF' : bodyInk, autoFit: 'none' };
        }
    }
    return 156 + rows.length * rowHeight;
}

function architecture(slide) {
    text(slide, 'blueprint-caption', '板屏操作与共用检测流水线', 75, 151, 1130, 42, 29, true);
    const stages = [
        ['输入与原帧', '图片 / 录像 / 摄像头\nID · 时间 · 源代次'],
        ['共用检测', '最新帧邮箱\n预处理 → NPU → 后处理'],
        ['结果有效性', '原帧绑定 · TTL\n故障与恢复检查'],
        ['提示与输出', '报警 · 对应帧截图\n本机JPEG / Web / 录像'],
    ];
    for (const [index, [title, content]] of stages.entries()) {
        const left = 75 + index * 296;
        text(slide, 'stage-' + index, title, left, 240, 258, 50, 32, true, ink);
        text(slide, 'stage-detail-' + index, content, left, 311, 250, 100, 25);
        if (index < stages.length - 1) text(slide, 'flow-' + index, '→', left + 255, 251, 40, 48, 32, false, ink);
    }
    text(slide, 'native-control', 'Qt板屏App → 本机令牌服务 → 开始 / 停止检测', 75, 466, 1130, 50, 28, true);
    text(slide, 'wifi-control', '用户手动WiFi连接；本地检测可离线。帧与资源日志贯穿检测路径。', 75, 536, 1130, 73, 26);
}

for (const [index, content] of authored.entries()) {
    const slide = presentation.slides.add();
    slide.background.fill = paper;
    slide.speakerNotes.text = clean(content.notes) + '\n\n依据：\n' + content.sources +
        '\n\n维护正文：docs/proposal/开题PPT逐页内容与演讲稿.md，第' + (index + 1) + '页';
    text(slide, 'page-number', String(index + 1).padStart(2, '0') + ' / 12', 1115, 663, 100, 25, 17, false, muted);
    if (index === 0) {
        text(slide, 'cover-title', '基于RK3568的离线安全帽检测\n与状态闭环系统设计与评估', 75, 172, 1130, 205, 56, true, ink);
        text(slide, 'cover-subtitle', '现成YOLOv5社区模型的离线部署、时效与故障状态评价', 75, 414, 1130, 60, 30);
        text(slide, 'cover-date', '开题报告 · 2026年11月', 75, 550, 1130, 43, 25, false, muted);
        continue;
    }
    text(slide, 'slide-title', content.title, 75, 64, 1130, 66, 44, true, ink);
    if (index === 4) {
        architecture(slide);
        continue;
    }
    if (index === 1) {
        const items = content.body.split('\n').filter(line => /^- /.test(line)).map(line => line.slice(2));
        bullets(slide, 'requirements', items, 75, 165, 635, 457, 26);
        await image(slide, path.join(repo, 'docs/experiments/2026-11-14/web-board-preview.jpg'), 748, 181, 452, 338,
            '前期真实NPU录像回放预览，非物理摄像头现场验收');
        text(slide, 'recorded-preview-label', '前期实测预览 · 真实NPU录像回放', 748, 546, 452, 64, 22, false, muted);
        continue;
    }
    if (index === 9) {
        const items = content.body.split('\n').filter(line => /^- /.test(line)).map(line => line.slice(2));
        bullets(slide, 'native-app-results', items, 75, 164, 760, 342, 26);
        const foot = content.body.split('\n').filter(line => line.trim() && !/^- /.test(line)).join('\n');
        text(slide, 'app-acceptance-limit', clean(foot), 75, 532, 760, 109, 23, false, muted);
        // The original Qt content grab has transparency. Keep the source
        // image bytes, and show its dark display surface behind that evidence.
        slide.shapes.add({ geometry: 'rect', name: 'screen-background',
            position: { left: 891, top: 149, width: 307, height: 473 }, fill: '#000000',
            line: { fill: 'none', width: 0, style: 'solid' } });
        await image(slide, path.join(repo, 'docs/experiments/2026-11-15/native-longrun-1/screen.png'), 891, 149, 307, 473,
            '原生Qt App长跑末段内容区域，非物理屏幕照片');
        continue;
    }
    const parsed = markdownTable(content.body);
    if (parsed) {
        const top = table(slide, parsed.rows, index === 10 ? 73 : 67);
        if (parsed.remainder) text(slide, 'table-context', clean(parsed.remainder), 75, top + 23, 1130, 635 - top - 23, 25);
    } else {
        const items = content.body.split('\n').filter(line => /^- /.test(line)).map(line => line.slice(2));
        const remaining = content.body.split('\n').filter(line => line.trim() && !/^- /.test(line)).join('\n\n');
        if (items.length) {
            bullets(slide, 'body-bullets', items, 75, 169, 1130, 356, 30);
            if (remaining) text(slide, 'body-context', clean(remaining), 75, 550, 1130, 85, 26, false, muted);
        } else {
            const paragraphs = content.body.split(/\n\s*\n/).map(clean).filter(Boolean);
            paragraphs.forEach((paragraph, n) => text(slide, 'body-paragraph-' + n, paragraph, 75, 164 + n * 143, 1130, 116, 30));
        }
    }
}

await (await PresentationFile.exportPptx(presentation)).save(draftPath);
const nativeTableSlides = [3, 4, 7, 8, 9, 11];
const finalized = await finalizePresentation({ workspaceDir: repo, candidatePath: draftPath, finalPath,
    pythonExecutable: runtimePython, integrityValidatorPath: path.join(skillDir, 'container_tools/inspect_presentation_package_integrity.py'),
    layoutValidatorPath: path.join(skillDir, 'container_tools/inspect_presentation_layout_geometry.py'),
    layoutArgs: ['--expected-slide-size-emu', '12192000,6858000', '--validate-bullet-geometry', '--validate-heading-fit',
        ...nativeTableSlides.flatMap(number => ['--require-native-table-slide', String(number)])],
    explicitTotalSlideCount: 12, requiredNativeTableOwnerSlides: nativeTableSlides,
    requiredNativeChartOwnerSlides: [], fontPolicy: { basis: 'design', families: [fontFamily] },
    verifyArtifactToolImport: true, receiptPath: path.join(build, 'validation.json') });

for (const [index, slide] of presentation.slides.items.entries()) {
    const preview = await presentation.export({ slide, format: 'png', scale: 1 });
    await fs.writeFile(path.join(build, 'previews', 'slide-' + String(index + 1).padStart(2, '0') + '.png'),
        new Uint8Array(await preview.arrayBuffer()));
}
const outputBytes = await fs.readFile(finalPath);
const summary = { path: path.relative(repo, finalPath).replaceAll('\\', '/'), slides: authored.length,
    source_sha256: createHash('sha256').update(source).digest('hex'),
    sha256: createHash('sha256').update(outputBytes).digest('hex'), bytes: outputBytes.length,
    font: fontFamily, editable_table_slides: nativeTableSlides, notes: 12, finalized: Boolean(finalized) };
await fs.writeFile(path.join(build, 'build-summary.json'), JSON.stringify(summary, null, 2));
console.log(JSON.stringify(summary, null, 2));
