import gtk
import os.path

import settings

'''Registers new icons with gtk.  Tries to use already existing icons
if they are available, otherwise it loads them from files.'''

ICON_INFO = [
  ('picty-rotate-left', 'picty-rotate-left.png'),
  ('picty-rotate-right', 'picty-rotate-right.png'),
  ('picty-sidebar', 'picty-sidebar.png'),
  ('picty-image-crop', 'picty-image-crop.png'),
  ('picty-image-rotate', 'picty-image-rotate.png'),
  ('picty-image-write', 'picty-image-write.png'),
  ('picty-transfer', 'picty-transfer.png'),
  ('picty-map', 'picty-map.png'),
  ]

filename=os.path.abspath(__file__)
if filename.startswith('/usr/share/picty/picty/register_icons.py'):
    icon_path='/usr/share/picty/icons/'
else:
    icon_path=os.path.join(os.path.split(os.path.split(os.path.split(filename)[0])[0])[0],'icons/')
print 'REGISTERING ICONS IN',icon_path

def register_iconset(icon_info):
  iconfactory = gtk.IconFactory()
  stock_ids = gtk.stock_list_ids()
  for stock_id, file in icon_info:
      # only load image files when our stock_id is not present
      if stock_id not in stock_ids:
          pixbuf = gtk.gdk.pixbuf_new_from_file(icon_path+file)
          iconset = gtk.IconSet(pixbuf)
          iconfactory.add(stock_id, iconset)
  iconfactory.add_default()

register_iconset(ICON_INFO)