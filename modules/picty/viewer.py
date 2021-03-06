#!/usr/bin/python

'''

    picty
    Copyright (C) 2013  Damien Moore

License:

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import threading
import time
import datetime

import gobject
import gtk
import pango

gobject.threads_init()

import settings
from fstools import io
import imagemanip
import pluginmanager
import metadata
import viewsupport
from uitools import overlay_widgets


class ImageLoader:
    '''
    Class to load full size images into memory on a background thread,
    notifying the viewer on completion
    '''
    def __init__(self,viewer):
        self.thread=threading.Thread(target=self._background_task)
        self.item=None
        self.sizing=None
        self.zoom='fit'
        self.memimages = []
        self.max_memimages=2
        self.vlock=threading.Lock() #viewer lock -- main thread should acquire lock before touch item image properties (image, qimage, size etc)
        self.viewer=viewer
        self.want_transforms=True
        self.event=threading.Event()
        self.exit=False
        self.plugin=None
        self.thread.start()

    def update_image_size(self,width,height,zoom='fit'):
        self.vlock.acquire()
        self.sizing=(width,height)
        self.zoom=zoom
        self.vlock.release()
        self.event.set()

    def quit(self):
        self.vlock.acquire()
        self.exit=True
        self.vlock.release()
        self.event.set()
        while self.thread.isAlive():
            time.sleep(0.1)

    def set_item(self,collection,item,sizing=None,zoom='fit',want_transfoms=True):
        self.vlock.acquire()
        self.collection=collection
        if self.item!=item and 'original_image' in dir(self.item):
            del self.item.original_image
        self.item=item
        self.sizing=sizing ##if sizing is none, zoom is ignored
        self.zoom=zoom ##zoom is either 'fit' or a floating point number for scaling, 1= 1 image pixel: 1 screen pixel; 2= 1 image pixel:2 screen pixel; 0.5 = 2 image pixel:1 screen pixel, so typically zoom<=1
        self.want_transforms=want_transfoms
        self.vlock.release()
        self.event.set()

    def set_plugin(self,plugin):
        self.vlock.acquire()
        self.plugin=plugin
        self.vlock.release()

    def release_plugin(self,plugin):
        self.vlock.acquire()
        if plugin==self.plugin:
            self.plugin=None
        self.vlock.release()

    def transform_image(self, want_transforms=True):
        '''
        reapply the transforms to the image
        '''
        self.vlock.acquire()
        self.item.image = None
        self.want_transforms = want_transforms
        self.vlock.release()
        self.event.set()

    def add_transform(self,name,params):
        imagemanip.transformer.add_transform(self.item,name,params,self.collection)
        #gobject.idle_add(self.viewer.ImageUpdated,self.item)

    def _background_task(self):
        ##todo: this code is horrible! clean it up
        self.vlock.acquire()
        while 1:
            if (self.sizing or self.item) and 'image' in dir(self.item) and self.item.image==None:
                self.event.set()
            else:
                self.event.clear()
            self.vlock.release()
            self.event.wait() #waits for a request for an image to be loaded by the main thread
            self.vlock.acquire()
            item=self.item
            self.vlock.release()
            if self.exit:
                return
            time.sleep(0.02)
            if not item:
                self.vlock.acquire()
                continue
            if item.meta==None:
                self.collection.load_metadata(item)
            if not item.image:
                def interrupt_cb():
                    return self.item.uid==item.uid
                print 'Image Viewer - LOADING IMAGE',item,'transforms',self.want_transforms
                self.collection.load_image(item,interrupt_cb,apply_transforms=self.want_transforms) #todo: load as draft if zoom not required (but need to keep draft status of image to avoid problems)
                if not item.image:
                    gobject.idle_add(self.viewer.ImageLoaded,item) #todo: change to image load failed? (the handler currently checks the status so that's enough anyway)
                    self.vlock.acquire()
                    continue
                gobject.idle_add(self.viewer.ImageLoaded,item)
            self.vlock.acquire()
            if self.sizing and self.sizing[0]>0 and self.sizing[1]>0:
                # determine the require width and height of the scaled image
                if self.zoom=='fit':
                    (w,h)=self.sizing
                else:
                    (iw,ih)=item.image.size
                    w=int(self.zoom*iw)
                    h=int(self.zoom*ih)
                # now size the image, allowing the plugins to do something before or after the operation
                print 'Image Viewer - SIZING',self.zoom,self.sizing
                if not self.plugin or not self.plugin.t_viewer_sizing((w,h),self.zoom,item):
                    imagemanip.size_image(item,self.sizing,False,self.zoom)
                    if self.plugin:
                        self.plugin.t_viewer_sized((w,h),self.zoom,item)
                gobject.idle_add(self.viewer.ImageSized,item,self.zoom,self.sizing)


class ImageViewer(gtk.VBox):
    TARGET_TYPE_URI_LIST = 0
    TARGET_TYPE_IMAGE = 2
    def __init__(self,worker,toolbar,viewer_window,click_callback=None,key_press_callback=None):
        gtk.VBox.__init__(self)
        self.toolbar=toolbar
        self.viewer_window = viewer_window
        self.il=ImageLoader(self)
        self.imarea=overlay_widgets.DrawableOverlayHover()
        self.imarea.add_with_bg(toolbar,0,1,0,None)
        self.imarea.connect("draw",self.draw)

        self.imarea.set_property("can-focus",True)
        self.worker=worker
        self.plugin_controller=None
        self.mouse_hover=False
        self.mouse_hover_pos=None
        self.command_highlight_ind=-1
        self.command_highlight_bd=False
        self.zoom_level='fit'
        self.zoom_position=(0,0) #either center or a tuple of left/top coordinates
        self.zoom_position_request=None

        self.freeze_image_refresh=False
        self.change_block=False

        self.vscrolladj=gtk.Adjustment()
        self.hscrolladj=gtk.Adjustment()
        self.vscroll=gtk.VScrollbar(self.vscrolladj)
        self.hscroll=gtk.HScrollbar(self.hscrolladj)
        self.vscrolladj.connect("value-changed",self.scroll_signal,True)
        self.hscrolladj.connect("value-changed",self.scroll_signal,False)
        self.imarea.add_events(gtk.gdk.SCROLL_MASK)
        self.imarea.connect("scroll-event",self.scroll_signal_pane)
        self.scroll_inc = 15

        self.imarea.show_all()

        self.image_box = gtk.VBox()
        self.image_box.pack_start(self.imarea)
        self.image_box.show()

        #Add scrollbars
        self.image_table=gtk.Table(rows=2,columns=2,homogeneous=False) ##plugins can add widgets to the box
        self.image_table.attach(self.image_box, 0, 1, 0, 1,
                       xoptions=gtk.EXPAND|gtk.FILL, yoptions=gtk.EXPAND|gtk.FILL, xpadding=0, ypadding=0)
        self.image_table.attach(self.vscroll, 1, 2, 0, 1,
                       xoptions=0, yoptions=gtk.EXPAND|gtk.FILL, xpadding=0, ypadding=0)
        self.image_table.attach(self.hscroll, 0, 1, 1, 2,
                       xoptions=gtk.EXPAND|gtk.FILL, yoptions=0, xpadding=0, ypadding=0)
        self.image_table.show()


        self.vpane=gtk.VPaned()
        self.vpane.add1(self.image_table) ##plugins can add widgets with add2
        self.pack_start(self.vpane)
        self.vpane.show()

        #self.imarea.add_events(gtk.gdk.EXPOSURE_MASK)
        self.conf_id=self.imarea.connect("size-allocate",self.size_signal)
        #self.imarea.connect("realize",self.realize_signal)
        self.connect("destroy", self._destroy)
        #self.imarea.add_events(gtk.gdk.SCROLL_MASK)
        #self.imarea.add_events(gtk.gdk.BUTTON_MOTION_MASK)
        self.imarea.add_events(gtk.gdk.BUTTON_PRESS_MASK)
        self.imarea.add_events(gtk.gdk.BUTTON_RELEASE_MASK)
        self.imarea.connect("button-press-event",self.button_press)
        self.imarea.connect("button-release-event",self.button_press)

        self.imarea.add_events(gtk.gdk.KEY_PRESS_MASK)
        self.imarea.add_events(gtk.gdk.KEY_RELEASE_MASK)

        self.imarea.add_events(gtk.gdk.POINTER_MOTION_MASK)
        self.imarea.connect("motion-notify-event",self.mouse_motion_signal)
        #self.imarea.add_events(gtk.gdk.ENTER_NOTIFY_MASK)
        #self.imarea.connect("enter-notify-event",self.mouse_enter_signal)
        self.imarea.add_events(gtk.gdk.LEAVE_NOTIFY_MASK)
        self.imarea.connect("leave-notify-event",self.mouse_leave_signal)

        self.target_list=[('image-filename', gtk.TARGET_SAME_APP, self.TARGET_TYPE_IMAGE)] #("XdndDirectSave0", 0, self.TARGET_TYPE_XDS),
        self.target_list=gtk.target_list_add_uri_targets(self.target_list,self.TARGET_TYPE_URI_LIST)
        #target_list=gtk.target_list_add_text_targets(target_list,self.TARGET_TYPE_URI_LIST)
        self.imarea.drag_source_set(gtk.gdk.BUTTON1_MASK,
                  self.target_list,gtk.gdk.ACTION_COPY)#| gtk.gdk.ACTION_MOVE)

##        target_list=[('tag-tree-row', gtk.TARGET_SAME_APP, 0)]
##        target_list=gtk.target_list_add_uri_targets(target_list,1)
##        self.imarea.drag_dest_set(gtk.DEST_DEFAULT_ALL,
##                target_list,
##                gtk.gdk.ACTION_DEFAULT | gtk.gdk.ACTION_COPY)

        self.imarea.connect("drag-data-get",self.drag_get_signal)
##        self.imarea.connect("drag-begin", self.drag_begin_signal) ##not needed when using drag_source_set
##        self.imarea.connect("drag-end",self.drag_end_signal)
##        self.imarea.connect("drag-data-received",self.drag_receive_signal)
        self.imarea.connect("drag-motion",self.drag_motion_signal)
        self.imarea.connect("drag-leave",self.drag_leave_signal)
        #self.imarea.drag_source_set_icon_stock('browser-drag-icon')


        if click_callback:
            self.imarea.connect_after("button-press-event",click_callback)
        if key_press_callback:
            self.imarea.connect("key-press-event",key_press_callback)
            self.imarea.connect("key-release-event",key_press_callback)

        pluginmanager.mgr.register_callback('t_collection_item_metadata_changed',self.meta_changed)

#        self.imarea.set_size_request(128,96)
        self.item=None
        self.browser=None
        self.last_sizing = None
        self.freeze_image_refresh = False
        self.freeze_image_resize = False
        self.fullscreen = False

    def plugin_request_control(self,plugin,force=False):
        '''
        normally called by plugin to request exclusive access to image rendering and loading events
        '''
        if self.plugin_controller:
            if not self.plugin_controller.viewer_release() and not force:
                return False
        print 'Image viewer: yielding control to plugin',plugin
        self.plugin_controller=plugin
        self.imarea.drag_source_unset()
        self.il.set_plugin(plugin)
        self.resize_and_refresh_view()
        self.imarea.hide_child(self.toolbar)
        return True

    def request_plugin_release(self,force=False):
        '''
        normally called by framework when user tries to navigate away from image
        plugin will receive request to release drawing control and should normally
        obey the request by calling plugin_release
        '''
        if not self.plugin_controller:
            self.imarea.unhide_child(self.toolbar)
            return True
        if not self.plugin_controller.viewer_release() and not force:
            return False
        self.imarea.unhide_child(self.toolbar)
        return True

    def plugin_release(self,plugin):
        '''
        called by plugin to relinquish control of the drawing and image loading/sizing events
        '''
        if plugin!=self.plugin_controller:
            return False
        self.plugin_controller=None
        self.imarea.drag_source_set(gtk.gdk.BUTTON1_MASK,
                  self.target_list,gtk.gdk.ACTION_COPY)#| gtk.gdk.ACTION_MOVE)
        self.il.release_plugin(plugin)
        self.imarea.grab_focus()
        self.imarea.unhide_child(self.toolbar)
        return True

    def ImageFullscreen(self,callback,is_already_fullscreen=False):
        if self.freeze_image_refresh or self.freeze_image_resize:
            return
        if self.fullscreen:
            return
        self.fullscreen=True
        screen = self.viewer_window.get_screen()
        if screen is not None:
            w=screen.get_width()
            h=screen.get_height()
            if w>0 and h>0:
                self.resize_and_refresh_view(w,h)
        self.fullscreen_callback=callback
        if self.imarea.window is not None:
            self.fullscreen_size_hint=self.imarea.window.get_size()
        else:
            self.fullscreen_size_hint = (0,0)
        self.freeze_image_refresh=True
#        if not is_already_fullscreen:
#            self.freeze_image_resize=True

    def ImageNormal(self,callback,is_already_fullscreen=False):
        if self.freeze_image_refresh or self.freeze_image_resize:
            return
        if not self.fullscreen:
            return
        self.fullscreen=False
        w,h = self.fullscreen_size_hint
        self.fullscreen_callback=callback
        self.resize_and_refresh_view(w,h)
        self.freeze_image_refresh=True
#        if not is_already_fullscreen:
#            self.freeze_image_resize=True

    def _destroy(self,event):
        self.request_plugin_release(True)
        self.il.quit()
        return False

    def ImageSized(self,item,zoom=None,sizing=None):
        self.last_sizing = (zoom,sizing)
        if not self.imarea.window:
            return
        if item.image==False:
            return
        if item.uid==self.item.uid:
            if zoom!=self.zoom_level:
                if zoom=='fit':
                    self.zoom_level=zoom
                    self.zoom_position=(0,0)
                else:
                    self.zoom_position=self.zoom_position_request
                    self.zoom_level=zoom
            self.update_scrollbars()
            self.toolbar.update_status(self)
            self.redraw_view()
        else:
            print 'WARNING: Sized wrong item',item.uid
        if self.freeze_image_refresh:
            self.fullscreen_callback()
            self.freeze_image_refresh=False

    def ImageLoaded(self,item):
        if 'image' in item.__dict__ and item.image not in (None,False):
            self.item.image.histo = item.image.histogram()

    def ImageUpdated(self,item):
        if self.browser!=None:
            self.browser.redraw_view()

    def SetItem(self,item,browser=None,collection=None):
        if not self.request_plugin_release():
            return False
        if not pluginmanager.mgr.callback_all_until_false('viewer_item_opening',item):
            return False
        self.zoom_level='fit'
        self.zoom_position=(0,0)
        self.hide_scrollbars()
        self.item=item
        self.collection=collection
        self.browser=browser
        self.il.set_item(collection,item,(self.get_size()[0],self.get_size()[1]),zoom=self.zoom_level)
        self.redraw_view()
        self.toolbar.update_status(self)
        return True

    def get_size(self):
        rect = self.imarea.get_allocation()
        return rect.width,rect.height
        try:
            rect = self.imarea.get_allocation()
            return rect.width,rect.height
        except:
            return -1,-1

#    def mouse_motion_signal(self,obj,event):
#        '''callback when mouse moves in the viewer area (updates image overlay as necessary)'''
#        if self.item!=None:
#            if not self.mouse_hover:
#                self.imarea.window.invalidate_rect((0,0,self.get_size()[0],self.get_size()[1]),True)
#            self.mouse_hover=True

    def meta_changed(self,collection,item,old_meta):
        if item!=self.item:
            return
        self.toolbar.update_status(self)
        try:
            ntr = item.meta['ImageTransforms']
        except:
            ntr = None
        try:
            otr = old_meta['ImageTransforms']
        except:
            otr = None
        if ntr!=otr:
            self.il.transform_image()

#    def mouse_enter_signal(self,obj,event):
#        '''callback when mouse moves in the viewer area (updates image overlay as necessary)'''
#        if self.item!=None:
#            if not self.mouse_hover:
#                self.image_box.queue_draw()
#            self.mouse_hover=True
#            self.mouse_hover_pos=(event.x,event.y)

    def mouse_motion_signal(self,obj,event):
        '''callback when mouse moves in the viewer area (updates image overlay as necessary)'''
        if self.item!=None:
            if not self.mouse_hover:
                self.redraw_view()
            if self.mouse_hover:
                bbottom=self.mouse_hover_pos[1]>=self.get_size()[1]-10
                btop=self.mouse_hover_pos[1]<self.get_size()[1]/3
            else:
                bbottom=False
                btop=False
            self.mouse_hover=True
            self.mouse_hover_pos=(event.x,event.y)
            if self.mouse_hover:
                abottom=self.mouse_hover_pos[1]>=self.get_size()[1]-10
                atop=self.mouse_hover_pos[1]<self.get_size()[1]/3
            else:
                abottom=False
                atop=False
            if atop!=btop or abottom!=bbottom:
                self.redraw_view()

    def mouse_leave_signal(self,obj,event):
        '''callback when mouse leaves the viewer area (hides image overlays)'''
        al = self.imarea.get_allocation()
        if 0<=event.x<al.width and 0<=event.y<al.height:
            return
#        if event.mode!=gtk.gdk.CROSSING_NORMAL:
#            return
        if self.item!=None:
            if self.mouse_hover:
                self.image_box.queue_draw()
        self.mouse_hover=False
        self.mouse_hover_pos=None

    def button_press(self,widget,event):
        self.command_highlight_bd=False
        if self.item!=None and self.item.qview!=None and event.button==1 and event.type==gtk.gdk.BUTTON_PRESS:
            self.command_highlight_bd=True
            self.redraw_view()

    def window_state_changed(self, widget, event):
        if event.changed_mask & gtk.gdk.WINDOW_STATE_FULLSCREEN:
            self.freeze_image_resize=False
            self.redraw_view()

    def resize_and_refresh_view(self,w=None,h=None,zoom=None,force=False):
        #forces an image to be resized with a call to the worker thread
        if self.freeze_image_refresh or self.freeze_image_resize:
            return
        if zoom==None:
            zoom=self.zoom_level
        if w==None:
            w=self.get_size()[0]
        if h==None:
            h=self.get_size()[1]
        if not force and self.last_sizing == (zoom, (w,h)):
            return
        self.il.update_image_size(w,h,zoom)

    def redraw_view(self):
        #forces an image to be resized with a call to the worker thread
        if self.freeze_image_refresh:
            return
        self.image_box.queue_draw()

    def size_signal(self,obj,event):
        self.update_scrollbars()
        if self.zoom_level=='fit':
            self.resize_and_refresh_view()
        self.redraw_view()

    def draw(self,obj,event):
        if self.freeze_image_refresh:
            return
        drawable = self.imarea.window
        gc = drawable.new_gc()
        colormap=drawable.get_colormap()
        black = colormap.alloc('black')
        if self.fullscreen:
            drawable.set_background(black)

        drew_image=False
        if pluginmanager.mgr.callback_first('viewer_render_start',drawable,gc,self.item):
            return
        if self.item and self.item.qview:
            #want to render the onscreen portion of the scaled image to the drawable
            #there are three pixbuf containers to worry about: full size image, scaled image, drawable
            #scroll position is stored in full image units
            try:
                src_x,src_y = self.image_xy_to_scaled_image(*self.zoom_position)
            except:
                return
            w = min(self.item.qview.get_width() - src_x,self.get_size()[0])
            h = min(self.item.qview.get_height() - src_y,self.get_size()[1])
            dest_x, dest_y = self.image_xy_to_screen(*self.zoom_position)
            drawable.draw_pixbuf(gc,self.item.qview,src_x,src_y,dest_x,dest_y,w,h)
            drew_image=True
        elif self.item and self.item.thumb:
            iw,ih=self.item.thumb.get_width(),self.item.thumb.get_height()
            x=(self.get_size()[0]-iw)/2
            y=(self.get_size()[1]-ih)/2
            drawable.draw_pixbuf(gc, self.item.thumb, 0, 0,x,y)
            drew_image=True
        if drew_image and self.plugin_controller is None:
            if self.mouse_hover and self.mouse_hover_pos[1]<self.get_size()[1]-10:
                self.render_image_info(drawable,gc)
        pluginmanager.mgr.callback_first('viewer_render_end',drawable,gc,self.item)

    def render_image_info(self,drawable,gc):
        if self.item is None:
            return
        item = self.item
        size = item.image.size if item.image else None
        histo = False
        iw,ih = self.get_size()
        if 'image' in item.__dict__ and item.image is not None and 'histo' in item.image.__dict__:
            hg = imagemanip.graphical_histogram(item.image.histo,(iw-205,ih-133),(200,128),drawable)
            histo = True
        a,b=self.collection.get_viewer_text(item,size,self.zoom_level)
        if a or b:
            a=a.replace('&','&amp;')
            b=b.replace('&','&amp;')
            l=self.imarea.create_pango_layout('')
            if a and b:
                l.set_markup('<b><span size="12000">'+a+'</span></b>\n<span size="10000">'+b+'</span>')
            elif a:
                l.set_markup('<b><span size="12000">'+a+'</span></b>')
            elif b:
                l.set_markup('<span size="10000">'+b+'</span>')
            l.set_width((self.get_size()[0]-20)*pango.SCALE)
            l.set_wrap(pango.WRAP_WORD_CHAR)
            w,h=l.get_pixel_size()
            lx=5
            ly=max(self.get_size()[1]-10-h,10)
            if h>0:
                overlay_pb=gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB,True,8,w+10,h+10)
                overlay_pb.fill(0x0000007f)
                drawable.draw_pixbuf(None,overlay_pb,0,0,lx-5,ly-5,-1,-1)
            colormap=drawable.get_colormap()
            white = colormap.alloc_color('white')
            drawable.draw_layout(gc,lx,ly,l,white)


    def scroll_signal_pane(self,obj,event):
        '''scrolls the view on mouse wheel motion'''
        if event.state&gtk.gdk.CONTROL_MASK:
            if event.direction==gtk.gdk.SCROLL_UP:
                self.set_zoom('in')
            if event.direction==gtk.gdk.SCROLL_DOWN:
                self.set_zoom('out')
            return
        if event.state&gtk.gdk.SHIFT_MASK:
            adj=self.hscrolladj
        else:
            adj=self.vscrolladj
        if event.direction==gtk.gdk.SCROLL_UP:
            self.pan_image('up')
        if event.direction==gtk.gdk.SCROLL_DOWN:
            self.pan_image('down')

    def scroll_signal(self,obj,vertical):
        '''signal response when the scroll position changes'''
        self.zoom_position=(self.hscrolladj.get_value(),self.vscrolladj.get_value())
        self.redraw_view()

    def update_scrollbars(self):
        '''called to resync the scrollbars to changes in view geometry'''
        if self.zoom_level=='fit' or self.item==None or self.item.image==None:
            return self.hide_scrollbars()
        (iw,ih)=self.item.image.size
        if iw*self.get_zoom()<self.get_size()[0] and ih*self.get_zoom()<self.get_size()[1]:
            return self.hide_scrollbars()
        left = self.zoom_position[0]
        top = self.zoom_position[1]
        self.hscrolladj.set_all(
                value=left,
                lower=0,
                upper=iw,
                step_increment=self.get_size()[0]/self.zoom_level,
                page_increment=self.get_size()[0]/self.zoom_level, page_size=self.get_size()[0]/self.zoom_level)
        self.vscrolladj.set_all(
                value=top,
                lower=0,
                upper=ih,
                step_increment=self.get_size()[1]/self.zoom_level,
                page_increment=self.get_size()[1]/self.zoom_level, page_size=self.get_size()[1]/self.zoom_level)
        self.vscroll.show()
        self.hscroll.show()

    def hide_scrollbars(self):
        self.vscroll.hide()
        self.hscroll.hide()

    def pan_image(self,direction):
        if self.zoom_level=='fit':
            return False
        iw,ih=self.item.image.size
        if iw*self.get_zoom()<self.get_size()[0] and ih*self.get_zoom()<self.get_size()[1]:
            return False
        if direction=='left':
            value=self.hscrolladj.get_value()-self.scroll_inc/self.get_zoom()
            value=max(value,self.hscrolladj.get_lower())
            self.hscrolladj.set_value(value)
        if direction=='right':
            value=self.hscrolladj.get_value()+self.scroll_inc/self.get_zoom()
            value=min(value,self.hscrolladj.get_upper()-self.hscrolladj.get_page_size())
            self.hscrolladj.set_value(value)
        if direction=='up':
            value=self.vscrolladj.get_value()-self.scroll_inc/self.get_zoom()
            value=max(value,self.vscrolladj.get_lower())
            self.vscrolladj.set_value(value)
        if direction=='down':
            value=self.vscrolladj.get_value()+self.scroll_inc/self.get_zoom()
            value=min(value,self.vscrolladj.get_upper()-self.vscrolladj.get_page_size())
            self.vscrolladj.set_value(value)
        return True

    def set_zoom(self,zoom_level,x=None,y=None):
        '''
        zooms the image to the specified zoom_level centering at viewer position (x,y). zoom_level is one of:
            * 'fit' to fit within the viewer window;
            * 'in' to zoom in 10%;
            * 'out' to zoom out 10%; or
            * a double to specify a specific zoom ratio, which is a multiple of the full
              image size: 1=100%, 0.5 = 50%,  2.0 = 200% etc)
        '''
        if self.item==None or self.item.image==None:
            return
        if x==None:
            x=self.get_size()[0]/2
        if y==None:
            y=self.get_size()[1]/2

        (iw,ih)=self.item.image.size
        if zoom_level=='fit':
            pass
        else:
            self.zoom_level=self.get_zoom()
        if zoom_level=='in':
            zoom_level=self.zoom_level*1.2
        if zoom_level=='out':
            zoom_level=self.zoom_level/1.2
        self.zoom_position_request=self.get_position_for_new_zoom(zoom_level,(x,y))
        self.resize_and_refresh_view(zoom=zoom_level)

    def screen_xy_to_scaled_image(self,x,y):
        if self.item==None or self.item.image==None or self.item.qview==None:
            return
        (qw,qh)=(self.item.qview.get_width(),self.item.qview.get_height())
        gw=self.get_size()[0]
        gh=self.get_size()[1]
        z=self.get_zoom()
        x-=max((gw-qw)/2,0)
        y-=max((gh-qh)/2,0)
        x=int(x+self.zoom_position[0]*z)
        y=int(y+self.zoom_position[1]*z)
        return (x,y)

    def screen_xy_to_image(self,x,y):
        if self.item==None or self.item.image==None or self.item.qview==None:
            return
        (iw,ih)=self.item.image.size
        (qw,qh)=(self.item.qview.get_width(),self.item.qview.get_height())
        gw=self.get_size()[0]
        gh=self.get_size()[1]
        z=self.get_zoom()
        x-=max((gw-qw)/2,0)
        y-=max((gh-qh)/2,0)
        x=int(x/z+self.zoom_position[0])
        y=int(y/z+self.zoom_position[1])
        return (x,y)

    def scaled_image_xy_to_screen(self,x,y):
        if self.item==None or self.item.image==None or self.item.qview==None:
            return
        (qw,qh)=(self.item.qview.get_width(),self.item.qview.get_height())
        gw,gh=self.get_size()
        z=self.get_zoom()
        x=int(x-self.zoom_position[0]*z)+max((gw-qw)/2,0)
        y=int(y-self.zoom_position[1]*z)+max((gh-qh)/2,0)
        return (x,y)

    def image_xy_to_screen(self,x,y):
        if self.item==None or self.item.image==None or self.item.qview==None:
            return
        (iw,ih)=self.item.image.size
        (qw,qh)=(self.item.qview.get_width(),self.item.qview.get_height())
        gw,gh=self.get_size()
        z=self.get_zoom()
        x=int((x-self.zoom_position[0])*z)+max((gw-qw)/2,0)
        y=int((y-self.zoom_position[1])*z)+max((gh-qh)/2,0)
        return (x,y)

    def get_zoom(self):
        '''
        returns the numeric level of the zoom (even if zoom_level is 'fit')
        '''
        if self.zoom_level=='fit':
            try:
                (iw,ih)=self.item.image.size
                return 1.0*self.item.qview.get_width()/iw
            except:
                return None
        else:
            return self.zoom_level

    def image_xy_to_scaled_image(self,x,y):
        z=self.get_zoom()
        return (int(x*z), int(y*z))

    def scaled_image_xy_to_image(self,x,y):
        z=self.get_zoom()
        return (int(x/z), int(y/z))

    def get_position_for_new_zoom(self,new_zoom,center_xy):
        if new_zoom=='fit':
            return (0,0)
        old_zoom=self.get_zoom()
        iw,ih=self.item.image.size
        qw,qh=(self.item.qview.get_width(),self.item.qview.get_height())
        gw,gh=self.get_size()
        if center_xy==None:
            center_xy=(gw/2,gh/2)
        cx,cy=center_xy
        ox,oy=self.screen_xy_to_image(cx,cy) #translate the center to image coordinates
        x = max(min(ox - gw/(2*new_zoom),iw-gw/new_zoom),0)
        y = max(min(oy - gh/(2*new_zoom),ih-gh/new_zoom),0)
        return (x,y)

    def toolbar_click(self,widget,callback):
        callback(widget,self.item)

    def switch_image_edit(self,image_edit_choice):
        if image_edit_choice == 'Original':
            self.il.transform_image(False)
        if image_edit_choice == 'Edited':
            self.il.transform_image(True)
        self.toolbar.update_status(self)

#    def drag_begin_signal(self, widget, drag_context):
#        '''
#        callback when user has started dragging one or more items in the browser's image area
#        '''
#        if self.item is not None:
#            return True

    def drag_get_signal(self, widget, drag_context, selection_data, info, timestamp):
        '''
        callback triggered to set the selection_data payload
        (viewer is the source of the drop)
        '''
        if self.item is None:
            return
        if info == self.TARGET_TYPE_IMAGE:
            selection_data.set('image-filename', 8, self.item.uid)
        if info == self.TARGET_TYPE_URI_LIST:
            #TODO: this doesn't work because protocal isn't local
#            if drag_context.protocol == gtk.gdk.DRAG_PROTO_LOCAL:
#                self.collection.get_path(self.item)
#                uri=io.get_uri(path)
#                selection_data.set_uris([uri])
#                print 'Internal Drag Drop from viewer using uri',uri
#                print 'Protocol',drag_context.protocol, gtk.gdk.DRAG_PROTO_LOCAL
#                return
            size=None
            if settings.dragdrop_resize:
                size=(settings.dragdrop_max_size,settings.dragdrop_max_size)
            path = imagemanip.get_jpeg_or_png_image_file(self.item,self.collection,size,settings.dragdrop_strip_metadata,settings.dragdrop_apply_edits)
            if path is None:
                return
            uri=io.get_uri(path)
            selection_data.set_uris([uri])
            print 'Drag Drop from viewer using uri',uri

    def drag_motion_signal(self, widget, drag_context, x, y, timestamp):
        self.redraw_view() ##todo: could do some calcs to see if anything actually needs to be redrawn

    def drag_leave_signal(self, widget, drag_context, timestamp):
        self.redraw_view()
