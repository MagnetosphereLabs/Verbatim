/* Verbatim native compositor blur bridge. SPDX-License-Identifier: MIT */
#include <gtk/gtk.h>
#include <gdk/wayland/gdkwayland.h>
#include <wayland-client.h>
#include <math.h>
#include <string.h>
#include "background-effect-client.h"
#include "kde-blur-client.h"

typedef struct {
    struct wl_display *display;
    struct wl_surface *surface;
    struct wl_event_queue *queue;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct ext_background_effect_manager_v1 *manager;
    struct ext_background_effect_surface_v1 *effect;
    struct org_kde_kwin_blur_manager *kde_manager;
    struct org_kde_kwin_blur *kde_effect;
    uint32_t capabilities;
    guint dispatch;
} Blur;
static void capabilities(void *data, struct ext_background_effect_manager_v1 *manager, uint32_t flags) {
    (void)manager;
    ((Blur *)data)->capabilities = flags;
}
static const struct ext_background_effect_manager_v1_listener effects_listener = {capabilities};
static void global(void *data, struct wl_registry *registry, uint32_t name, const char *interface, uint32_t version) {
    Blur *blur = data;
    if (!strcmp(interface, "wl_compositor")) {
        blur->compositor = wl_registry_bind(registry, name, &wl_compositor_interface, MIN(version, 1));
        wl_proxy_set_queue((struct wl_proxy *)blur->compositor, blur->queue);
    } else if (!strcmp(interface, "ext_background_effect_manager_v1")) {
        blur->manager = wl_registry_bind(registry, name, &ext_background_effect_manager_v1_interface, 1);
        wl_proxy_set_queue((struct wl_proxy *)blur->manager, blur->queue);
        ext_background_effect_manager_v1_add_listener(blur->manager, &effects_listener, blur);
    } else if (!strcmp(interface, "org_kde_kwin_blur_manager")) {
        blur->kde_manager = wl_registry_bind(registry, name, &org_kde_kwin_blur_manager_interface, 1);
        wl_proxy_set_queue((struct wl_proxy *)blur->kde_manager, blur->queue);
    }
}
static void removed(void *data, struct wl_registry *registry, uint32_t name) { (void)data; (void)registry; (void)name; }
static const struct wl_registry_listener registry_listener = {global, removed};
static gboolean dispatch(gpointer data) {
    Blur *blur = data;
    /* GDK owns display reads; this callback dispatches only our private queue. */
    if (wl_display_dispatch_queue_pending(blur->display, blur->queue) < 0) {
        blur->dispatch = 0;
        return G_SOURCE_REMOVE;
    }
    return G_SOURCE_CONTINUE;
}
static void destroy(gpointer data) {
    Blur *blur = data;
    if (blur->dispatch) g_source_remove(blur->dispatch);
    if (blur->effect) ext_background_effect_surface_v1_destroy(blur->effect);
    if (blur->kde_effect) org_kde_kwin_blur_release(blur->kde_effect);
    if (blur->manager) ext_background_effect_manager_v1_destroy(blur->manager);
    /* KDE's manager v1 has no destructor request. */
    if (blur->kde_manager) wl_proxy_destroy((struct wl_proxy *)blur->kde_manager);
    if (blur->compositor) wl_compositor_destroy(blur->compositor);
    if (blur->registry) wl_registry_destroy(blur->registry);
    if (blur->queue) wl_event_queue_destroy(blur->queue);
    g_free(blur);
}
int verbatim_blur(GtkWindow *window, int width, int height, int enabled) {
    GdkSurface *surface = gtk_native_get_surface(GTK_NATIVE(window));
    if (!surface || !GDK_IS_WAYLAND_SURFACE(surface)) return 0;
    struct wl_surface *wl_surface = gdk_wayland_surface_get_wl_surface(surface);
    Blur *blur = g_object_get_data(G_OBJECT(window), "verbatim-native-blur");
    if (blur && blur->surface != wl_surface) {
        g_object_set_data(G_OBJECT(window), "verbatim-native-blur", NULL);
        blur = NULL;
    }
    if (!blur) {
        blur = g_new0(Blur, 1);
        blur->surface = wl_surface;
        blur->display = gdk_wayland_display_get_wl_display(gdk_surface_get_display(surface));
        blur->queue = wl_display_create_queue(blur->display);
        blur->registry = wl_display_get_registry(blur->display);
        wl_proxy_set_queue((struct wl_proxy *)blur->registry, blur->queue);
        wl_registry_add_listener(blur->registry, &registry_listener, blur);
        if (wl_display_roundtrip_queue(blur->display, blur->queue) < 0 ||
            wl_display_roundtrip_queue(blur->display, blur->queue) < 0 || !blur->compositor ||
            (!(blur->manager && (blur->capabilities & 1)) && !blur->kde_manager)) {
            destroy(blur);
            return 0;
        }
        if (blur->manager && (blur->capabilities & 1)) {
            blur->effect = ext_background_effect_manager_v1_get_background_effect(blur->manager, wl_surface);
            wl_proxy_set_queue((struct wl_proxy *)blur->effect, blur->queue);
        } else {
            blur->kde_effect = org_kde_kwin_blur_manager_create(blur->kde_manager, wl_surface);
            wl_proxy_set_queue((struct wl_proxy *)blur->kde_effect, blur->queue);
        }
        blur->dispatch = g_timeout_add(500, dispatch, blur);
        g_object_set_data_full(G_OBJECT(window), "verbatim-native-blur", blur, destroy);
    }
    if (!enabled) {
        if (blur->effect) ext_background_effect_surface_v1_set_blur_region(blur->effect, NULL);
        if (blur->kde_effect) {
            org_kde_kwin_blur_manager_unset(blur->kde_manager, wl_surface);
            org_kde_kwin_blur_release(blur->kde_effect);
            blur->kde_effect = NULL;
        }
    } else {
        struct wl_region *region = wl_compositor_create_region(blur->compositor);
        const int radius = 24;
        for (int y = 0; y < height - 17; y++) {
            int dy = y < radius ? radius - y : (y >= height - 17 - radius ? y - (height - 18 - radius) : 0);
            int inset = dy ? radius - (int)sqrt(MAX(0, radius * radius - dy * dy)) : 0;
            wl_region_add(region, 8 + inset, 7 + y, MAX(1, width - 16 - 2 * inset), 1);
        }
        if (blur->effect) ext_background_effect_surface_v1_set_blur_region(blur->effect, region);
        if (!blur->effect && !blur->kde_effect) {
            blur->kde_effect = org_kde_kwin_blur_manager_create(blur->kde_manager, wl_surface);
            wl_proxy_set_queue((struct wl_proxy *)blur->kde_effect, blur->queue);
        }
        if (blur->kde_effect) {
            org_kde_kwin_blur_set_region(blur->kde_effect, region);
            org_kde_kwin_blur_commit(blur->kde_effect);
        }
        wl_region_destroy(region);
    }
    wl_display_flush(blur->display);
    return 1;
}
