#!/usr/bin/env python3
"""Local ebook extraction and review checkpoints. Never calls a model or network."""
import argparse
import copy
import hashlib
from html.parser import HTMLParser
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

import yaml

KINDS = {'chapter', 'preface', 'afterword', 'appendix', 'part', 'toc', 'frontmatter', 'index', 'other'}
DIRS = ['00-source', '01-meta', '02-chapters/assets', '03-book-summary', '04-book-mind',
        '05-chapter-summary', '06-publication', '99-raw']


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def digest(paths):
    return hashlib.sha256('\n'.join(sha(p) for p in paths).encode()).hexdigest()


def pandoc(data, from_fmt, to_fmt, cwd=None, extra=()):
    if not shutil.which('pandoc'):
        raise ValueError('Missing pandoc; use an available equivalent and preserve the same review gates.')
    result = subprocess.run(['pandoc', '-f', from_fmt, '-t', to_fmt, '--wrap=none', *extra],
                            input=data, text=True, capture_output=True, cwd=cwd)
    if result.returncode:
        raise ValueError('Pandoc conversion failed: ' + result.stderr[-1500:])
    return result.stdout


def safe_name(value):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '-', value).strip(' .')
    return value[:140] or '未命名书籍'


def local_asset(base, target, root):
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme or parsed.netloc:
        raise ValueError('Non-local image reference: ' + target)
    p = (base / urllib.parse.unquote(parsed.path)).resolve()
    if not p.is_relative_to(root.resolve()) or not p.is_file():
        raise ValueError('Missing or out-of-project image: ' + target)
    return p


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def plain(obj):
    parts = []
    for node in walk(obj):
        if node.get('t') == 'Str':
            parts.append(node['c'])
        elif node.get('t') in ('Space', 'SoftBreak', 'LineBreak'):
            parts.append(' ')
    return ''.join(parts).strip()


def image_refs(markdown):
    """Include both Markdown images and HTML image syntax emitted for sized figures."""
    ast = json.loads(pandoc(markdown, 'gfm', 'json'))
    refs = []
    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == 'img':
                attrs = dict(attrs)
                if attrs.get('src'):
                    refs.append(attrs['src'])
    parser = Images()
    for node in walk(ast):
        if node.get('t') == 'Image':
            refs.append(node['c'][2][0])
        elif node.get('t') in ('RawBlock', 'RawInline') and node['c'][0] == 'html':
            parser.feed(node['c'][1])
    return list(dict.fromkeys(refs))


def epub_info(source):
    ns = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container',
          'o': 'http://www.idpf.org/2007/opf', 'd': 'http://purl.org/dc/elements/1.1/'}
    with zipfile.ZipFile(source) as z:
        total = 0
        for i in z.infolist():
            p = PurePosixPath(i.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in i.filename or i.flag_bits & 1:
                raise ValueError('Unsafe or encrypted EPUB member: ' + i.filename)
            total += i.file_size
        if total > 1024 ** 3 or len(z.infolist()) > 30000:
            raise ValueError('EPUB exceeds default resource budget; review before adapting limits.')
        if 'META-INF/encryption.xml' in z.namelist():
            enc = ET.fromstring(z.read('META-INF/encryption.xml'))
            algorithms = [e.get('Algorithm', '') for e in enc.iter() if e.tag.endswith('EncryptionMethod')]
            if any(a not in ('http://www.idpf.org/2008/embedding', 'http://ns.adobe.com/pdf/enc#RC') for a in algorithms):
                raise ValueError('Encrypted EPUB content; request an accessible source.')
        container = ET.fromstring(z.read('META-INF/container.xml'))
        rootfile = container.find('.//c:rootfile', ns)
        if rootfile is None:
            raise ValueError('EPUB has no OPF rootfile.')
        opf_path = rootfile.get('full-path')
        opf_bytes = z.read(opf_path)
        opf = ET.fromstring(opf_bytes)
        def vals(tag):
            return [''.join(e.itertext()).strip() for e in opf.findall('.//d:' + tag, ns)]
        info = {k: vals(k) for k in ('title', 'creator', 'publisher', 'language', 'identifier', 'date')}
        role_map = {e.get('refines', '').lstrip('#'): (e.text or '').strip()
                    for e in opf.findall('.//o:meta', ns) if e.get('property') == 'role'}
        authors, translators = [], []
        for e in opf.findall('.//d:creator', ns) + opf.findall('.//d:contributor', ns):
            role = e.get('{http://www.idpf.org/2007/opf}role') or role_map.get(e.get('id'), '')
            value = ''.join(e.itertext()).strip()
            if role == 'trl':
                translators.append(value)
            elif e.tag.endswith('creator') and role in ('', 'aut'):
                authors.append(value)
        info['author'], info['translator'] = authors, translators
        items = {e.get('id'): e.attrib for e in opf.findall('.//o:manifest/o:item', ns)}
        spine = [items[e.get('idref')] for e in opf.findall('.//o:spine/o:itemref', ns)]
        navigation = {'spine': spine, 'entries': []}
        for item in items.values():
            item_url = urllib.parse.urlsplit(item['href'])
            if item_url.scheme or item_url.netloc:
                raise ValueError('External EPUB manifest resource is not supported: ' + item['href'])
            href = posixpath.normpath(str(PurePosixPath(opf_path).parent / urllib.parse.unquote(item_url.path)))
            if href.startswith('../') or href.startswith('/'):
                raise ValueError('EPUB resource escapes the archive root: ' + item['href'])
            if item.get('media-type') in ('application/xhtml+xml', 'image/svg+xml'):
                raw = z.read(href).decode('utf-8', errors='replace')
                if re.search(r'(?:src|href)\s*=\s*["\'](?:https?:)?//[^"\']+', raw, re.I):
                    # Hyperlinks are fine, remote image resources are not.
                    if re.search(r'<(?:img|image)\b[^>]*(?:src|href)\s*=\s*["\'](?:https?:)?//', raw, re.I):
                        raise ValueError('EPUB includes a remote image; supply a local source image.')
            if 'nav' in item.get('properties', '').split() or item.get('media-type') == 'application/x-dtbncx+xml':
                nav = ET.fromstring(z.read(href))
                for e in nav.iter():
                    if e.tag.endswith('}a'):
                        navigation['entries'].append({'title': ''.join(e.itertext()).strip(), 'target': e.get('href'), 'source': href})
                    elif e.tag.endswith('}navPoint'):
                        label = e.find('{*}navLabel/{*}text')
                        target = e.find('{*}content')
                        if label is not None and target is not None:
                            navigation['entries'].append({'title': label.text, 'target': target.get('src'), 'source': href})
        return info, navigation, opf_bytes


def extract_epub(source, book, navigation):
    media = book / '99-raw/epub-media'
    result = subprocess.run(['pandoc', str(source), '-f', 'epub', '-t', 'json', '--extract-media=' + str(media)],
                            text=True, capture_output=True)
    if result.returncode:
        raise ValueError('EPUB conversion failed: ' + result.stderr[-1500:])
    doc = json.loads(result.stdout)
    for node in walk(doc):
        if node.get('t') == 'Image':
            target = node['c'][2][0]
            p = local_asset(book, target, book)
            dest = book / '02-chapters/assets' / (sha(p)[:20] + p.suffix.lower())
            if not dest.exists():
                shutil.copyfile(p, dest)
            node['c'][2][0] = 'assets/' + dest.name
    units = []
    def flatten(blocks, source_id=''):
        for block in blocks:
            if block.get('t') == 'Div':
                attr, children = block['c']
                flatten(children, attr[0] or source_id)
            else:
                units.append({'index': len(units), 'source_anchor': source_id, 'block': block,
                              'preview': plain(block)})
    flatten(doc['blocks'])
    doc['blocks'] = [u['block'] for u in units]
    write_json(book / '99-raw/document.json', doc)
    candidates = [{'start': u['index'], 'level': u['block']['c'][0], 'title': plain(u['block']['c'][2]),
                   'anchor': u['block']['c'][1][0]} for u in units if u['block'].get('t') == 'Header']
    report = {'format': 'EPUB', 'unit_count': len(units), 'spine_items': len(navigation['spine']),
              'extraction_reviewed': False, 'warnings': ['Verify spine/nav coverage, notes, images, tables and chapter boundaries.']}
    return units, candidates, report


def extract_pdf(source, book):
    import fitz
    doc = fitz.open(source)
    if doc.needs_pass:
        raise ValueError('PDF requires a password; provide an accessible file.')
    units, candidates, low, page_reports = [], [], [], []
    pattern = re.compile(r'^(?:第[一二三四五六七八九十百零〇\d]+[章节部篇]|chapter\s+\w+|序言|前言|后记|附录)', re.I)
    for page_number, page in enumerate(doc, 1):
        blocks = page.get_text('dict', sort=True)['blocks']
        texts = [b for b in blocks if b['type'] == 0]
        chars = sum(len(s['text']) for b in texts for l in b['lines'] for s in l['spans'])
        has_images = any(b['type'] == 1 for b in blocks)
        drawings = bool(page.get_drawings())
        if chars < 30 and (has_images or drawings):
            low.append(page_number)
        preview = None
        if drawings or chars < 30:
            previews = book / '99-raw/page-previews'
            previews.mkdir(exist_ok=True)
            preview = previews / f'p{page_number:04d}.png'
            page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).save(preview)
        for b in blocks:
            unit = {'index': len(units), 'page': page_number, 'bbox': list(b['bbox'])}
            if b['type'] == 0:
                lines = [''.join(s['text'] for s in line['spans']) for line in b['lines']]
                value = '\n'.join(lines).strip()
                if not value:
                    continue
                size = max((s['size'] for l in b['lines'] for s in l['spans']), default=0)
                # Escape syntax before exposing source as Markdown; retain original lines.
                escaped = re.sub(r'([\\`*_{}\[\]<>])', r'\\\1', value)
                escaped = re.sub(r'(?m)^([#>+\-])', r'\\\1', escaped)
                escaped = re.sub(r'(?m)^(\d+)\.', r'\1\\.', escaped)
                unit.update(markdown=escaped, text=value, font_size=round(size, 2))
                if pattern.match(value) and len(value) < 150:
                    candidates.append({'start': len(units), 'title': value, 'page': page_number, 'font_size': size})
            elif b['type'] == 1:
                raw = b['image']
                ext = b.get('ext', 'png')
                name = hashlib.sha256(raw).hexdigest()[:20] + '.' + ext
                (book / '02-chapters/assets' / name).write_bytes(raw)
                unit.update(markdown=f'![原书插图，PDF物理第{page_number}页](assets/{name})', image=name)
            else:
                continue
            units.append(unit)
        page_reports.append({'page': page_number, 'text_chars': chars, 'vector_drawings': drawings,
                             'preview': str(preview.relative_to(book)) if preview else None})
    write_json(book / '99-raw/navigation.json', {'bookmarks': doc.get_toc(), 'pages': page_reports})
    report = {'format': 'PDF', 'unit_count': len(units), 'page_count': len(doc), 'ocr_required_pages': low,
              'extraction_reviewed': False, 'warnings': ['Verify columns, headers/footers, equations, tables and vector charts against page images.']}
    return units, candidates, report


def metadata(source, info):
    first = lambda key: next(iter(info.get(key, [])), None)
    title = first('title')
    date = first('date') or ''
    match = re.match(r'^(\d{4})(?:-(\d{2}))?', date)
    identifiers = info.get('identifier', [])
    def valid_isbn(value):
        value = re.sub(r'^(?:urn:)?isbn\s*:', '', value, flags=re.I).replace('-', '').replace(' ', '').upper()
        if re.fullmatch(r'\d{13}', value) and value.startswith(('978', '979')):
            return value if sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(value)) % 10 == 0 else None
        if re.fullmatch(r'\d{9}[\dX]', value):
            return value if sum((10 if n == 'X' else int(n)) * (10 - i) for i, n in enumerate(value)) % 11 == 0 else None
        return None
    isbn = next((parsed for v in identifiers if (parsed := valid_isbn(v))), None)
    return {'title_zh': title if title and re.search(r'[\u3400-\u9fff]', title) else None,
            'title_original': None, 'author': info.get('author', []), 'translator': info.get('translator', []),
            'publisher': first('publisher'), 'imprint': None, 'edition': None,
            'publication_year': int(match[1]) if match else None,
            'publication_month': int(match[2]) if match and match[2] and 1 <= int(match[2]) <= 12 else None,
            'isbn': isbn, 'source_language': first('language'), 'output_language': 'zh-CN',
            'book_type': None, 'source_format': source.suffix[1:].upper(), 'original_publication_year': None,
            'source_file': source.name}


def prepare(args):
    source = Path(args.source).resolve()
    if source.suffix.lower() not in ('.epub', '.pdf') or not source.is_file():
        raise ValueError('Supply an existing EPUB or PDF.')
    if not shutil.which('pandoc'):
        raise ValueError('Missing dependency: pandoc')
    info, nav, opf = epub_info(source) if source.suffix.lower() == '.epub' else ({}, {}, b'')
    meta = metadata(source, info)
    title = args.title or next(iter(info.get('title', [])), source.stem)
    year = args.year or meta['publication_year'] or '年份不详'
    book = Path(args.root).resolve() / safe_name(f'{title}｜{year}')
    book.mkdir(parents=True, exist_ok=False)
    for d in DIRS:
        (book / d).mkdir(parents=True, exist_ok=True)
    dest = book / '00-source' / ('source' + source.suffix.lower())
    shutil.copyfile(source, dest)
    if sha(source) != sha(dest):
        raise ValueError('Source copy hash mismatch.')
    (book / '01-meta/book-meta.md').write_text('---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) +
        '---\n\n元数据草稿：已提取字段来自原文件内部元数据，待与版权页核验。未知字段保留 null。\n', encoding='utf-8')
    manifest = {'version': 1, 'title': title, 'source_sha256': sha(dest), 'source_file': str(dest.relative_to(book)),
                'original_filename': source.name, 'stages': {}, 'chapters': []}
    write_json(book / '99-raw/manifest.json', manifest)
    try:
        if source.suffix.lower() == '.epub':
            (book / '99-raw/package.opf').write_bytes(opf)
            write_json(book / '99-raw/navigation.json', nav)
            units, candidates, report = extract_epub(dest, book, nav)
        else:
            units, candidates, report = extract_pdf(dest, book)
        if not units:
            raise ValueError('No content extracted; inspect the original, OCR or DRM status.')
        write_json(book / '99-raw/units.json', units)
        write_json(book / '99-raw/toc-candidates.json', candidates)
        write_json(book / '99-raw/extraction-report.json', report)
        manifest['stages']['extraction'] = {'status': 'awaiting_review', 'units': len(units)}
    except Exception as exc:
        manifest['stages']['extraction'] = {'status': 'blocked', 'reason': str(exc)}
        write_json(book / '99-raw/manifest.json', manifest)
        raise
    write_json(book / '99-raw/manifest.json', manifest)
    print(str(book))


def context(book):
    book = Path(book).resolve()
    manifest = read_json(book / '99-raw/manifest.json')
    if sha(book / manifest['source_file']) != manifest['source_sha256']:
        raise ValueError('Archived source changed.')
    return book, manifest


def split(args):
    book, manifest = context(args.book)
    if manifest['chapters'] or list((book / '02-chapters').glob('*.md')):
        raise ValueError('Already split; preserve prior outputs before revising boundaries.')
    plan = read_json(args.plan)
    report = read_json(book / '99-raw/extraction-report.json')
    if plan.get('source_sha256') != manifest['source_sha256'] or plan.get('reviewed') is not True or plan.get('extraction_reviewed') is not True:
        raise ValueError('A source-matched, reviewed extraction and split plan are required.')
    if report.get('ocr_required_pages') and report.get('ocr_reviewed') is not True:
        raise ValueError('OCR review still required for pages: ' + str(report['ocr_required_pages']))
    units = read_json(book / '99-raw/units.json')
    chapters = plan.get('chapters', [])
    starts = [c['start'] for c in chapters]
    if (not starts or starts[0] != 0 or any(type(s) is not int for s in starts)
            or starts != sorted(set(starts)) or starts[-1] >= len(units)):
        raise ValueError('Boundaries must be unique increasing unit indexes, starting at zero.')
    docpath = book / '99-raw/document.json'
    doc = read_json(docpath) if docpath.exists() else None
    counts, prepared = {}, []
    for n, c in enumerate(chapters):
        kind = c['kind']
        if kind not in KINDS or not isinstance(c['title'], str) or not c['title'].strip():
            raise ValueError('Invalid chapter kind/title.')
        counts[kind] = counts.get(kind, 0) + 1
        cid = ('ch' if kind == 'chapter' else kind) + f'{counts[kind]:02d}'
        end = starts[n + 1] if n + 1 < len(starts) else len(units)
        subset = units[c['start']:end]
        if doc:
            fragment = copy.deepcopy(doc)
            fragment['blocks'] = [u['block'] for u in subset]
            text = pandoc(json.dumps(fragment), 'json', 'gfm', book / '02-chapters')
        else:
            text = '\n\n'.join(f'<!-- PDF physical page {u["page"]}; unit {u["index"]} -->\n' + u['markdown'] for u in subset)
        if not text.strip():
            raise ValueError('Empty chapter extraction: ' + cid)
        # Let Pandoc parse image references so parentheses and escaped names remain correct.
        for target in image_refs(text):
            local_asset(book / '02-chapters', target, book / '02-chapters/assets')
        prepared.append((cid, text, {'id': cid, 'kind': kind, 'title': c['title'], 'start': c['start'], 'end': end,
                                    'source': f'02-chapters/{cid}.md', 'summary': f'05-chapter-summary/{cid}-summary.md',
                                    'status': 'pending'}))
    for cid, text, record in prepared:
        (book / record['source']).write_text(text.rstrip() + '\n', encoding='utf-8')
        record['source_sha256'] = sha(book / record['source'])
        manifest['chapters'].append(record)
    write_json(book / '99-raw/toc.json', manifest['chapters'])
    manifest['stages']['extraction'] = {'status': 'reviewed', 'units': len(units), 'chapter_parts': len(prepared)}
    report['extraction_reviewed'] = True
    write_json(book / '99-raw/extraction-report.json', report)
    write_json(book / '99-raw/manifest.json', manifest)
    print(f'Saved {len(prepared)} original parts.')


def export_source(args):
    book, manifest = context(args.book)
    if not manifest['chapters']:
        raise ValueError('Split the source first.')
    # Preserve Markdown bytes except image paths, which must resolve from 99-raw.
    pieces = []
    for c in manifest['chapters']:
        text = (book / c['source']).read_text(encoding='utf-8')
        text = text.replace('](assets/', '](../02-chapters/assets/').replace('](<assets/', '](<../02-chapters/assets/')
        text = re.sub(r'(\bsrc=["\'])assets/', r'\1../02-chapters/assets/', text)
        pieces.append(text)
    target = book / '99-raw/book-original.md'
    target.write_text('\n\n'.join(pieces), encoding='utf-8')
    print(str(target))


def source_paths(book, manifest):
    paths = [book / manifest['source_file']] + [book / c['source'] for c in manifest['chapters']]
    # Include assets so correcting an image invalidates summaries using source material.
    return paths + sorted(p for p in (book / '02-chapters/assets').rglob('*') if p.is_file())


def stage_paths(book, manifest, stage, cid=None):
    if stage == 'book-summary':
        return source_paths(book, manifest), book / '03-book-summary/book-summary.md'
    if stage == 'mind':
        return source_paths(book, manifest) + [book / '03-book-summary/book-summary.md', book / '99-raw/mind.json'], book / '04-book-mind/book-mind.html'
    c = next((c for c in manifest['chapters'] if c['id'] == cid), None)
    if c is None:
        raise ValueError('Unknown chapter: ' + str(cid))
    refs = image_refs((book / c['source']).read_text(encoding='utf-8'))
    images = [local_asset(book / '02-chapters', target, book) for target in refs]
    return [book / c['source'], book / '01-meta/book-meta.md', *images], book / c['summary']


def stamp(args):
    book, manifest = context(args.book)
    if not args.reviewed or manifest['stages'].get('extraction', {}).get('status') != 'reviewed':
        raise ValueError('Complete extraction and content review before recording this checkpoint.')
    inputs, output = stage_paths(book, manifest, args.stage, args.id)
    if not output.is_file() or not output.read_text(encoding='utf-8').strip():
        raise ValueError('Missing or empty output: ' + str(output))
    checkpoint = {'status': 'reviewed', 'input_sha256': digest(inputs), 'output_sha256': sha(output), 'reviewed_at': int(time.time())}
    if args.stage == 'chapter-summary':
        next(c for c in manifest['chapters'] if c['id'] == args.id).update(checkpoint)
    else:
        manifest['stages'][args.stage] = checkpoint
    manifest['stages'].pop('publication', None)
    write_json(book / '99-raw/manifest.json', manifest)
    print('Recorded review: ' + args.stage + (' ' + args.id if args.id else ''))


def skip(args):
    book, manifest = context(args.book)
    c = next((c for c in manifest['chapters'] if c['id'] == args.id), None)
    if c is None or c['kind'] == 'chapter' or not args.reason.strip():
        raise ValueError('Only non-chapter parts may be skipped, with a specific reason.')
    c.update(status='skipped', reason=args.reason, skip_source_sha256=sha(book / c['source']))
    manifest['stages'].pop('publication', None)
    write_json(book / '99-raw/manifest.json', manifest)


def verify_checkpoint(book, manifest, stage, cid=None):
    record = (next(c for c in manifest['chapters'] if c['id'] == cid) if cid else manifest['stages'].get(stage, {}))
    inputs, output = stage_paths(book, manifest, stage, cid)
    if (record.get('status') != 'reviewed' or not output.is_file()
            or record.get('input_sha256') != digest(inputs) or record.get('output_sha256') != sha(output)):
        raise ValueError('Missing, unreviewed or stale output: ' + str(output))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('source'); p.add_argument('--root', required=True); p.add_argument('--title'); p.add_argument('--year', type=int)
    p.set_defaults(func=prepare)
    p = sub.add_parser('split'); p.add_argument('book'); p.add_argument('--plan', required=True); p.set_defaults(func=split)
    p = sub.add_parser('export-source'); p.add_argument('book'); p.set_defaults(func=export_source)
    p = sub.add_parser('stamp'); p.add_argument('book'); p.add_argument('--stage', choices=['book-summary', 'mind', 'chapter-summary'], required=True)
    p.add_argument('--id'); p.add_argument('--reviewed', action='store_true'); p.set_defaults(func=stamp)
    p = sub.add_parser('skip'); p.add_argument('book'); p.add_argument('--id', required=True); p.add_argument('--reason', required=True); p.set_defaults(func=skip)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError, KeyError, StopIteration, zipfile.BadZipFile, ET.ParseError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    main()
