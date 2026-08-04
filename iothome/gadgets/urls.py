from django.urls import path

from . import views

app_name = "gadgets"

urlpatterns = [
    path("types/", views.GadgetTypeListView.as_view(), name="type-list"),
    path("", views.GadgetListView.as_view(), name="list"),
    path("<uuid:uid>/", views.GadgetDetailView.as_view(), name="detail"),
    path("<uuid:uid>/commands/", views.GadgetCommandsView.as_view(), name="commands"),
    path("<uuid:uid>/disable/", views.DisableGadgetView.as_view(), name="disable"),
    path(
        "<uuid:uid>/provisioning/",
        views.ProvisioningView.as_view(),
        name="provisioning",
    ),
]
