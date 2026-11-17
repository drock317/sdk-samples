#!/usr/bin/env python3
"""Install the Dynamic Web Ports kit into an SDK samples workspace.

Copies, into the TARGET workspace:

  .kiro/steering/dynamic-web-port.md      always-on guardrail for the agent
  .kiro/skills/web-port/SKILL.md          the integration procedure
  .kiro/skills/web-port/web_port.py       canonical module the skill copies from
  <template-dir>/web_port.py              so new web apps get it with the template
                                          (skipped if that folder does not exist)

Safe by default:
  * verifies src/web_port.py against its published SHA-256 first
  * never overwrites a file that differs (reports it) unless --force
  * --dry-run shows the plan and changes nothing

Usage:
  python3 install.py --target /path/to/sdk-samples
  python3 install.py --target /path/to/sdk-samples --dry-run
  python3 install.py --target /path/to/sdk-samples --force
  python3 install.py --target . --template-dir apps/templates/web_app_template

Standard library only. Python 3.8+.
"""
import argparse
import hashlib
import os
import shutil
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE_DIR = os.path.join('apps', 'templates', 'web_app_template')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()


def verify_module():
    """The module must match its published checksum before we distribute it."""
    module = os.path.join(KIT, 'src', 'web_port.py')
    with open(module + '.sha256') as f:
        published = f.read().split()[0]
    actual = sha256(module)
    if actual != published:
        print('ERROR: src/web_port.py does not match src/web_port.py.sha256')
        print('  published: {}'.format(published))
        print('  actual:    {}'.format(actual))
        print('Refusing to install a module that differs from the validated '
              'copy.')
        sys.exit(2)
    return module


def build_plan(target, template_dir, module, skip_template):
    """Return a list of (label, source, destination)."""
    plan = [
        ('steering',
         os.path.join(KIT, 'kiro', 'steering', 'dynamic-web-port.md'),
         os.path.join(target, '.kiro', 'steering', 'dynamic-web-port.md')),
        ('skill',
         os.path.join(KIT, 'kiro', 'skills', 'web-port', 'SKILL.md'),
         os.path.join(target, '.kiro', 'skills', 'web-port', 'SKILL.md')),
        ('skill module', module,
         os.path.join(target, '.kiro', 'skills', 'web-port', 'web_port.py')),
    ]
    if not skip_template:
        tdir = os.path.join(target, template_dir)
        if os.path.isdir(tdir):
            plan.append(('template module', module,
                         os.path.join(tdir, 'web_port.py')))
        else:
            print('note: template folder not found, skipping template copy: '
                  '{}'.format(tdir))
    return plan


def classify(src, dst):
    if not os.path.exists(dst):
        return 'new'
    return 'same' if sha256(src) == sha256(dst) else 'differs'


def main():
    ap = argparse.ArgumentParser(
        description='Install the Dynamic Web Ports kit into a workspace.')
    ap.add_argument('--target', required=True,
                    help='SDK samples workspace root to install into')
    ap.add_argument('--template-dir', default=DEFAULT_TEMPLATE_DIR,
                    help='web app template folder, relative to target '
                         '(default: %(default)s)')
    ap.add_argument('--no-template', action='store_true',
                    help='do not copy web_port.py into the web app template')
    ap.add_argument('--dry-run', action='store_true',
                    help='show what would happen, change nothing')
    ap.add_argument('--force', action='store_true',
                    help='overwrite files that differ from the kit copy')
    args = ap.parse_args()

    target = os.path.abspath(args.target)
    if not os.path.isdir(target):
        print('ERROR: target is not a directory: {}'.format(target))
        return 2

    module = verify_module()
    plan = build_plan(target, args.template_dir, module, args.no_template)

    conflicts = 0
    print('Target: {}{}'.format(target, '  (dry run)' if args.dry_run else ''))
    for label, src, dst in plan:
        state = classify(src, dst)
        rel = os.path.relpath(dst, target)
        if state == 'same':
            print('  up to date   {:<16} {}'.format(label, rel))
            continue
        if state == 'differs' and not args.force:
            conflicts += 1
            print('  DIFFERS      {:<16} {}  (kept; use --force to '
                  'overwrite)'.format(label, rel))
            continue
        verb = 'would install' if args.dry_run else 'installed'
        if state == 'differs':
            verb = 'would overwrite' if args.dry_run else 'overwrote'
        print('  {:<12} {:<16} {}'.format(verb, label, rel))
        if not args.dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)

    if conflicts:
        print('\n{} file(s) differ from the kit and were left untouched. '
              'Review them, then re-run with --force if the kit copy should '
              'win.'.format(conflicts))
        return 1
    if args.dry_run:
        print('\nDry run only. Nothing was changed.')
    else:
        print('\nDone. Reload the Kiro window so it picks up the new steering '
              'and skill, then try the prompts in prompts/.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
