"""Offline legacy portrait parity harness; run in the same pinned runtime for both trees.

PR2_PARITY_OUTPUT selects the JSON destination. No providers or source-art fetches.
"""
from dataclasses import asdict
import hashlib, json, io, os
from pathlib import Path
import numpy as np
from PIL import Image
import main
from preset_registry import get_preset
from render_spec import canonicalize_config, compile_requirements
from v2_render import _request_config, _encode_webp
from discovery import DiscoveryMeta
from i18n import load_languages
load_languages()
rows=[]
configs=[]
for ref in ('clean-notch@1','clean-notch@2','clean-notch@3','clean-notch@4','prestige@1','prestige@2','prestige@3','minimalist@1','minimalist@2','minimalist@3','minimalist@4'):
 configs.append((ref,get_preset(ref).config))
for mode in range(6):
 for append in (range(4) if mode==3 else range(3) if mode==1 else ('rating','rating_year','year','sash','second_rating') if mode==4 else (0,)):
  raw={'rating_display_mode':mode,'sash_mode':'notch','sash_priority':'cult','sash_badge_style':'gold','score_out_of_10':True,'bar_score_out_of_10':True,'score_color_mode':2}
  if mode==1:raw['accent_bar_append_mode']=append
  elif mode==3:raw['minimalist_append_mode']=append
  elif mode==4:raw['bar_append']=append
  configs.append((f'custom-{mode}-{append}',canonicalize_config(raw)))
configs.append(('custom-left-legacy-default',canonicalize_config({'rating_display_mode':2,'sash_mode':'sash','sash_priority':'cult'})))
for name,spec in configs:
 for pattern in ('gradient','solid'):
  if pattern=='gradient':
   y,x=np.mgrid[0:750,0:500];arr=np.stack(((x*7+y*3)%256,(x+y*5)%256,(x*11+y)%256),axis=-1).astype(np.uint8)
   image=Image.fromarray(arr,'RGB').convert('RGBA')
  else:
   image=Image.new('RGBA',(500,750),(37,68,99,255))
  for locale in ('en','nl'):
   cfg=_request_config(main,spec,locale)
   out=main.build_poster(image.copy(),87,'Sci-Fi',cfg,discovery_meta=DiscoveryMeta(is_cult=True),release_year='1999',metacritic_score=73)
   payload=_encode_webp(out.convert('RGB'))
   rows.append({'id':f'{name}-{pattern}-{locale}','config_sha256':spec.sha256(),'requirements':asdict(compile_requirements(spec)),'pixels_sha256':hashlib.sha256(out.tobytes()).hexdigest(),'webp_sha256':hashlib.sha256(payload).hexdigest(),'webp_size':len(payload)})
Path(os.environ['PR2_PARITY_OUTPUT']).write_text(json.dumps(rows,sort_keys=True,indent=2)+'\n')
print(f'{len(rows)} renders')
