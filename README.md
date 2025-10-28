**Warning: At the moment this is just a work in progress/proof of concept piece of AI scripting prototype. Do not use for the time being unless you exactly know what you are doing. Use at your own risk!**

Live log DBus method_returns:
dbus-monitor --session "type='method_return'"

Switching KDE/GTK dialogues:
Using a KDE based distro (in a virtual machine) like the newest Kubuntu is recommended because it will have both the KDE and the GTK portal backends for the file dialogs preinstalled.
In /home/silver/.config/xdg-desktop-portal/portals.conf enter the following:

[preferred]
org.freedesktop.impl.portal.FileChooser=gtk

This will switch to the GTK dialog after restarting the portal services with:
systemctl --user restart xdg-desktop-portal
systemctl --user restart xdg-desktop-portal-gtk.service

By commenting the lines in the config or by replacing "gtk" with "kde", the portal will switch back to the KDE file dialog .

Shortcomings:
- Choosing the file type in the save dialog will only set the file extension, but the file type will still be regular RPP. There seems to be no ReaScript API Endpoint for saving files in a different format.
- Copy & Convert in the save project dialog is just an optical placeholder to demonstrate how it COULD look like. It doesn't actually do anything in this implementation.
- Many features like "Open in new tab" or setting the current session to the saved project after the saving dialog is implemented with hacky solutions (like opening the project again with a second call). This is because of the limitations of the ReaScript API. Those features are still implemented because this project is meant to be a proof of concept and starting point for an official implementation of xdg-desktop-portals Filechooser. An offical implementation would of course not suffer from the same limitations and would behave exactly like it always did.
