#!/usr/bin/env python3
"""Run inside a cmux terminal. Opens configured Lead and Team Runner workspaces."""
from pathlib import Path
import argparse,json,os,subprocess,sys

CMUX='/Applications/cmux.app/Contents/Resources/bin/cmux'
CENTRAL=Path(__file__).resolve().parent.parent

def run(*args):
    # CMUX_QUIET: cmux deprecation uyarisi stdout'a yazilip JSON'u bozuyor.
    env={**os.environ,'CMUX_QUIET':'1'}
    p=subprocess.run([CMUX,*args],text=True,capture_output=True,env=env)
    if p.returncode:raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p.stdout

def call_json(*args):
    output=run('--json',*args).strip()
    if not output:
        raise RuntimeError('cmux JSON çıktısı boş döndü: '+' '.join(args))
    return json.loads(output)

def titles(value):
    if isinstance(value,dict):
        result={value[k] for k in ['title','name'] if isinstance(value.get(k),str)}
        for child in value.values():result.update(titles(child))
        return result
    if isinstance(value,list):
        result=set()
        for child in value:result.update(titles(child))
        return result
    return set()

def find_workspace(value,name):
    if isinstance(value,dict):
        if value.get('title')==name or value.get('name')==name:
            return value
        for child in value.values():
            found=find_workspace(child,name)
            if found:return found
    elif isinstance(value,list):
        for child in value:
            found=find_workspace(child,name)
            if found:return found
    return None

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('project',help='ad: projects.json kaydindaki anahtar')
    p.add_argument('--only',choices=['control','delivery'])
    p.add_argument('--dry-run',action='store_true')
    args=p.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    import team
    registry=team.load_projects()
    if args.project not in registry:
        raise ValueError('Kayitli degil: '+args.project+' (projects.json)')
    root=Path(registry[args.project]['root'])
    config=json.loads((root/'.cmux/cmux.json').read_text())
    if args.dry_run:
        for c in config['commands']:
            workspace=c['workspace']
            if args.only and not workspace['name'].lower().endswith(args.only):
                continue
            print(workspace['name'],workspace['cwd'])
        return
    if not os.environ.get('CMUX_SURFACE_ID'):
        raise RuntimeError('Bu komutu cmux içindeki bir terminalde çalıştırın; erişim ayarını değiştirmeyin.')
    subprocess.run([CMUX,'reload-config'],check=True)
    workspace_list=call_json('workspace','list')
    existing=titles(workspace_list)
    for command in config['commands']:
        workspace=command['workspace']
        if args.only and not workspace['name'].lower().endswith(args.only):
            continue
        if workspace['name'] in existing:
            found=find_workspace(workspace_list,workspace['name']) or {}
            handle=next((found.get(k) for k in ['ref','id','workspaceId','workspace_id'] if found.get(k)),None)
            if args.only and handle:
                run('workspace','select',str(handle))
                print('Zaten açıktı, seçildi:',workspace['name'])
            else:
                print('Zaten açık:',workspace['name'])
            continue
        # Workspace create mutasyonunda bazı cmux sürümleri başarılı olduğu halde
        # stdout'u boş bırakıyor; burada JSON parse etmeyip yalnız exit kodunu denetle.
        run('workspace','create','--name',workspace['name'],'--cwd',workspace['cwd'],
            '--layout',json.dumps(workspace['layout']),'--focus','true' if args.only else 'false')
        print('Açıldı:',workspace['name'])
    print('Lead ve Team Runner yeni workspace ile otomatik başlar. Mevcut eski workspace açıksa kapatıp yeniden açın.')

if __name__=='__main__':
    try:main()
    except (RuntimeError,ValueError,KeyError,OSError,subprocess.CalledProcessError) as e:
        print('STOP:',e,file=sys.stderr);sys.exit(2)
