#!/usr/bin/env python3
"""Render reviewed book artifacts without rewriting prose or loading remote assets."""
import argparse
import base64
import html
from html.parser import HTMLParser
import io
import json
import mimetypes
from pathlib import Path
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

import yaml

from book_pipeline import (context, digest, local_asset, pandoc, read_json, sha,
                           source_paths, verify_checkpoint, write_json)

ASSETS = Path(__file__).resolve().parent.parent / 'assets'
E = lambda s: html.escape(str(s), quote=True)


def image_data(path):
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or ''
    if mime == 'image/svg+xml':
        root = ET.fromstring(raw)
        for el in root.iter():
            if el.tag.split('}')[-1].lower() in ('script', 'foreignobject'):
                raise ValueError('Active SVG is not allowed: ' + str(path))
            for key, value in el.attrib.items():
                key = key.split('}')[-1].lower()
                if key.startswith('on') or (key == 'href' and not value.startswith('#')):
                    raise ValueError('External or active SVG attribute: ' + str(path))
        if re.search(rb'url\s*\(\s*[\'"]?(?!#)|@import', raw, re.I):
            raise ValueError('SVG references require local review: ' + str(path))
    elif mime not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
        from PIL import Image
        out = io.BytesIO()
        Image.open(io.BytesIO(raw)).convert('RGBA').save(out, 'PNG')
        raw, mime = out.getvalue(), 'image/png'
    return 'data:' + mime + ';base64,' + base64.b64encode(raw).decode()


class SafeHTML(HTMLParser):
    TAGS = set('p h1 h2 h3 h4 h5 h6 ul ol li strong em b i code pre blockquote table thead tbody tfoot tr th td caption hr br a img figure figcaption sup sub s del div span section dl dt dd details summary'.split())
    VOID = {'img', 'br', 'hr'}
    DROP = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'form'}

    def __init__(self, base, book, prefix):
        super().__init__(convert_charrefs=True)
        self.base, self.book, self.prefix = base, book, prefix
        self.out, self.drop, self.images = [], 0, []

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP:
            self.drop += 1
            return
        if self.drop or tag not in self.TAGS:
            return
        attrs = dict(attrs)
        safe = {}
        for key in ('title', 'alt', 'colspan', 'rowspan', 'start', 'role'):
            if key in attrs:
                safe[key] = attrs[key]
        if attrs.get('id'):
            safe['id'] = self.prefix + '-' + attrs['id']
        if tag == 'img':
            target = attrs.get('src', '')
            path = local_asset(self.base, target, self.book)
            self.images.append(path)
            safe['src'] = image_data(path)
            safe['alt'] = attrs.get('alt', '')
            safe['loading'] = 'lazy'
        if tag == 'a':
            href = attrs.get('href', '')
            if href.startswith('#'):
                safe['href'] = '#' + self.prefix + '-' + href[1:]
            elif urllib.parse.urlsplit(href).scheme in ('https', 'http', 'mailto'):
                safe['href'] = href
                safe['rel'] = 'noreferrer noopener'
            elif href:
                # Keep link text, but do not leave a dependency on an unpublished source file.
                safe['title'] = attrs.get('title') or '原书引用：' + href
        if tag == 'details' and 'open' in attrs:
            safe['open'] = ''
        self.out.append('<' + tag + ''.join(' ' + k + '="' + E(v or '') + '"' for k, v in safe.items()) + '>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.DROP:
            self.drop = max(0, self.drop - 1)
            return
        if not self.drop and tag in self.TAGS and tag not in self.VOID:
            self.out.append('</' + tag + '>')

    def handle_data(self, data):
        if not self.drop:
            self.out.append(E(data))


def markdown_fragment(path, book, prefix):
    raw = pandoc(path.read_text(encoding='utf-8'), 'gfm', 'html5', path.parent)
    parser = SafeHTML(path.parent, book, prefix)
    parser.feed(raw)
    parser.close()
    return ''.join(parser.out), parser.images


def document(title, body, scripted=False):
    css = (ASSETS / 'reader.css').read_text(encoding='utf-8')
    js = (ASSETS / 'reader.js').read_text(encoding='utf-8') if scripted else ''
    return ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            '<title>' + E(title) + '</title><style>' + css + '</style></head><body>' + body +
            ('<script>' + js + '</script>' if js else '') + '</body></html>\n')


def mind_fragment(data):
    for key in ('title', 'subtitle', 'core', 'logic_title', 'takeaway'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError('mind.json requires a nonempty string: ' + key)
    if not isinstance(data.get('cards'), list) or not data['cards'] or not isinstance(data.get('logic'), list) or not data['logic']:
        raise ValueError('mind.json requires nonempty cards and logic lists.')
    cards = []
    for n, card in enumerate(data['cards'], 1):
        if not isinstance(card.get('title'), str) or not card['title'].strip() or not card.get('points'):
            raise ValueError('Every card needs a title and points.')
        points = []
        for p in card['points']:
            if not isinstance(p.get('label'), str) or not isinstance(p.get('text'), str) or not p['text'].strip():
                raise ValueError('Card point must contain string label and nonempty text.')
            points.append('<li><strong>' + E(p['label']) + '：</strong>' + E(p['text']) + '</li>')
        cards.append('<article><details open><summary><span class="num">' + f'{n:02d}' + '</span><span>' +
                     E(card['title']) + '</span><span class="chev" aria-hidden="true">⌄</span></summary><ul>' +
                     ''.join(points) + '</ul></details></article>')
    if any(not isinstance(s, str) or not s.strip() for s in data['logic']):
        raise ValueError('Logic steps must be nonempty strings.')
    steps = ''.join('<div class="step"><span>STEP ' + str(i) + '</span><strong>' + E(s) + '</strong></div>'
                    for i, s in enumerate(data['logic'], 1))
    return ('<div class="mind"><p class="dek">' + E(data['subtitle']) + '</p>'
            '<section class="core"><small>全书主旨</small><strong>' + E(data['core']) + '</strong></section>'
            '<div class="stem" aria-hidden="true"></div><section class="grid">' + ''.join(cards) + '</section>'
            '<section class="logic"><h3>' + E(data['logic_title']) + '</h3><div class="flow">' + steps + '</div>'
            '<p class="takeaway"><strong>一句话记忆：</strong>' + E(data['takeaway']) + '</p></section></div>')


def build_mind(args):
    book, manifest = context(args.book)
    verify_checkpoint(book, manifest, 'book-summary')
    data = read_json(book / '99-raw/mind.json')
    fragment = mind_fragment(data)
    body = '<main><header><p class="eyebrow">BOOK MIND MAP</p><h1>' + E(data['title']) + ' · 思维导图</h1></header><!--MIND_START-->' + fragment + '<!--MIND_END--></main>'
    target = book / '04-book-mind/book-mind.html'
    target.write_text(document(data['title'] + ' · 思维导图', body), encoding='utf-8')
    print(str(target))


def verified_inputs(book, manifest):
    paths = [verify_checkpoint(book, manifest, 'book-summary'), verify_checkpoint(book, manifest, 'mind'), book / '01-meta/book-meta.md']
    for c in manifest['chapters']:
        if c['status'] == 'skipped':
            if c['kind'] == 'chapter' or c.get('skip_source_sha256') != sha(book / c['source']) or not c.get('reason'):
                raise ValueError('Invalid or stale skip: ' + c['id'])
        else:
            paths.append(verify_checkpoint(book, manifest, 'chapter-summary', c['id']))
    return paths


def metadata(book):
    text = (book / '01-meta/book-meta.md').read_text(encoding='utf-8')
    match = re.match(r'^---\s*\n(.*?)\n---(?:\s*\n|$)', text, re.S)
    if not match:
        raise ValueError('book-meta.md must begin with YAML frontmatter.')
    data = yaml.safe_load(match[1])
    required = {'title_zh','title_original','author','translator','publisher','imprint','edition','publication_year',
                'publication_month','isbn','source_language','output_language','book_type','source_format',
                'original_publication_year','source_file'}
    if not isinstance(data, dict) or not required <= data.keys():
        raise ValueError('Incomplete metadata fields.')
    if not isinstance(data['author'], list) or not isinstance(data['translator'], list):
        raise ValueError('Authors and translators must be lists.')
    return data


def build_book(args):
    book, manifest = context(args.book)
    inputs = verified_inputs(book, manifest)
    meta = metadata(book)
    title = meta.get('title_zh') or meta.get('title_original') or manifest['title']
    slug = args.slug or 'book-' + manifest['source_sha256'][:12]
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}', slug):
        raise ValueError('Use a filesystem-safe ASCII slug.')
    summary, images = markdown_fragment(book / '03-book-summary/book-summary.md', book, 'bs')
    mind_html = (book / '04-book-mind/book-mind.html').read_text(encoding='utf-8')
    match = re.search(r'<!--MIND_START-->(.*?)<!--MIND_END-->', mind_html, re.S)
    if not match:
        raise ValueError('Rebuild the standalone mind page using the supplied renderer.')
    mind = match[1]
    if mind != mind_fragment(read_json(book / '99-raw/mind.json')):
        raise ValueError('Mind HTML no longer matches the reviewed card data.')
    chapter_html, links, skipped = [], [], []
    for c in manifest['chapters']:
        if c['status'] == 'skipped':
            skipped.append(c['title'])
            continue
        text, chapter_images = markdown_fragment(book / c['summary'], book, c['id'])
        images.extend(chapter_images)
        target_id = 'chapter-' + c['id']
        links.append('<li><a href="#' + target_id + '">' + E(c['title']) + '</a></li>')
        chapter_html.append('<details class="chapter" id="' + target_id + '"><summary><span>' + E(c['title']) +
                            '</span><span class="chev" aria-hidden="true">⌄</span></summary><div class="prose">' + text + '</div></details>')
    byline = []
    if meta.get('author'):
        byline.append('、'.join(meta['author']))
    if meta.get('publisher'):
        byline.append(str(meta['publisher']))
    if meta.get('publication_year'):
        byline.append(str(meta['publication_year']))
    body = ('<a class="skip-link" href="#book-summary">跳到正文</a><main id="top"><header class="book-header">'
            '<p class="eyebrow">BOOK READING</p><h1>' + E(title) + '</h1><p class="dek">' + E(' · '.join(byline)) + '</p>'
            '<p class="reader-note">全书总结 · 卡片式思维导图 · 逐章细读</p></header>'
            '<nav class="tabs" aria-label="阅读导航"><a href="#book-summary">全书总结</a><a href="#book-mind">思维导图</a><a href="#chapter-summaries">逐章细读</a></nav>'
            '<details class="contents"><summary>阅读目录</summary><ol>' + ''.join(links) + '</ol></details>'
            '<section class="reading-section" id="book-summary"><h2>全书总结</h2><div class="prose">' + summary + '</div></section>'
            '<section class="reading-section" id="book-mind"><h2>卡片式思维导图</h2>' + mind + '</section>'
            '<section class="reading-section" id="chapter-summaries"><div class="section-heading"><h2>逐章细读</h2>'
            '<div class="actions"><button type="button" data-open="true">展开全部</button><button type="button" data-open="false">收起全部</button>'
            '<button type="button" id="print-book">打印</button></div></div>' + ''.join(chapter_html) + '</section>')
    if skipped:
        body += '<p class="reader-note">仅归档原文的辅助材料：' + E('、'.join(skipped)) + '。</p>'
    body += '<footer><a href="#top">回到顶部 ↑</a></footer></main>'
    target = book / '06-publication' / (slug + '.html')
    target.write_text(document(title, body, scripted=True), encoding='utf-8')
    inputs += sorted(set(images)) + [ASSETS / 'reader.css', ASSETS / 'reader.js']
    manifest['stages']['publication'] = {'status': 'generated', 'file': str(target.relative_to(book)),
        'input_sha256': digest(inputs), 'output_sha256': sha(target), 'embedded_images': [str(p.relative_to(book)) for p in sorted(set(images))],
        'structure_checked': False, 'visual_review': 'pending'}
    write_json(book / '99-raw/manifest.json', manifest)
    print(str(target))


class Inspector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.links, self.errors, self.details, self.chapter_ids = set(), [], [], 0, []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            if attrs['id'] in self.ids:
                self.errors.append('Duplicate id: ' + attrs['id'])
            self.ids.add(attrs['id'])
        for key, value in attrs.items():
            if key.startswith('on'):
                self.errors.append('Unexpected event attribute: ' + key)
        if tag in ('iframe', 'object', 'embed', 'link', 'base'):
            self.errors.append('External-capable element: ' + tag)
        if tag == 'img' and not re.match(r'^data:image/(?:png|jpeg|gif|webp|svg\+xml);base64,', attrs.get('src', '')):
            self.errors.append('Image not embedded.')
        if tag in ('script', 'audio', 'video', 'source') and any(x in attrs for x in ('src', 'srcset', 'poster')):
            self.errors.append('Non-embedded runtime asset.')
        if tag == 'a' and attrs.get('href', '').startswith('#'):
            self.links.append(attrs['href'][1:])
        if tag == 'details' and attrs.get('class') == 'chapter':
            self.details += 1
            self.chapter_ids.append(attrs.get('id'))


def check(args):
    book, manifest = context(args.book)
    inputs = verified_inputs(book, manifest)
    publication = manifest['stages'].get('publication')
    if not publication:
        raise ValueError('No publication has been generated.')
    target = book / publication['file']
    inputs += [book / p for p in publication['embedded_images']] + [ASSETS / 'reader.css', ASSETS / 'reader.js']
    if digest(inputs) != publication['input_sha256'] or sha(target) != publication['output_sha256']:
        raise ValueError('Publication is stale or was modified; rebuild it.')
    text = target.read_text(encoding='utf-8')
    parser = Inspector(); parser.feed(text)
    parser.errors += ['Broken anchor: ' + x for x in parser.links if x not in parser.ids]
    expected = ['chapter-' + c['id'] for c in manifest['chapters'] if c['status'] != 'skipped']
    if parser.chapter_ids != expected:
        parser.errors.append('Chapter membership/order differs from the reviewed manifest.')
    styles = re.findall(r'<style[^>]*>(.*?)</style>', text, re.S | re.I)
    if styles != [(ASSETS / 'reader.css').read_text(encoding='utf-8')]:
        parser.errors.append('Unexpected stylesheet content.')
    if any(re.search(r'@import|url\s*\(', style, re.I) for style in styles):
        parser.errors.append('Unexpected CSS dependency; inspect before delivery.')
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', text, re.S | re.I)
    if scripts != [(ASSETS / 'reader.js').read_text(encoding='utf-8')]:
        parser.errors.append('Unexpected script content.')
    if parser.errors:
        raise ValueError('\n'.join(parser.errors))
    publication.update(status='structure_checked', structure_checked=True)
    write_json(book / '99-raw/manifest.json', manifest)
    print(json.dumps({'ok': True, 'chapter_summaries': parser.details, 'single_file': True,
                      'visual_review': publication.get('visual_review', 'pending')}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest='command', required=True)
    q = s.add_parser('mind'); q.add_argument('book'); q.set_defaults(func=build_mind)
    q = s.add_parser('book'); q.add_argument('book'); q.add_argument('--slug'); q.set_defaults(func=build_book)
    q = s.add_parser('check'); q.add_argument('book'); q.set_defaults(func=check)
    args = p.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError) as exc:
        p.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    main()
