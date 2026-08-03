from django.urls import path

from . import views

app_name = "purchases"

urlpatterns = [
    path("products/", views.ProductListView.as_view(), name="product-list"),
    path("products/<slug:slug>/", views.ProductDetailView.as_view(), name="product-detail"),
    path("orders/", views.OrderListView.as_view(), name="order-list"),
    path("orders/<uuid:uid>/", views.OrderDetailView.as_view(), name="order-detail"),
    path("checkout/", views.CheckoutView.as_view(), name="checkout"),
    path("payments/verify/", views.PaymentVerifyView.as_view(), name="payment-verify"),
]
