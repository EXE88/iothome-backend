"""Split an order's single product into lines, so a basket can hold several.

The generated order of operations dropped Order.product before OrderItem
existed, which would have thrown away every placed order. Here the table is
created first, the existing orders are copied into it one line each, and only
then do the old columns go.
"""

import django.db.models.deletion
from django.db import migrations, models


def orders_to_lines(apps, schema_editor):
    Order = apps.get_model("purchases", "Order")
    OrderItem = apps.get_model("purchases", "OrderItem")
    OrderItem.objects.bulk_create(
        OrderItem(
            order_id=order.pk,
            product_id=order.product_id,
            quantity=order.quantity,
            unit_price=order.unit_price,
        )
        for order in Order.objects.all().iterator()
    )


def lines_to_orders(apps, schema_editor):
    """Reverse: fold each order's first line back onto the order itself.

    An order with more than one line cannot be represented by the old schema,
    so the extra lines are lost. That is the honest reverse of this change,
    not a bug — it is why the forward direction is the one to keep.
    """
    Order = apps.get_model("purchases", "Order")
    for order in Order.objects.all().iterator():
        line = order.items.order_by("id").first()
        if line is None:
            continue
        order.product_id = line.product_id
        order.quantity = line.quantity
        order.unit_price = line.unit_price
        order.save(update_fields=["product", "quantity", "unit_price"])


class Migration(migrations.Migration):

    dependencies = [
        ("purchases", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity", models.PositiveSmallIntegerField(default=1)),
                ("unit_price", models.DecimalField(decimal_places=0, max_digits=12)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="purchases.order",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="order_items",
                        to="purchases.product",
                    ),
                ),
            ],
            options={
                "ordering": ("id",),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("order", "product"),
                        name="one_line_per_product_per_order",
                    )
                ],
            },
        ),
        migrations.RunPython(orders_to_lines, lines_to_orders),
        migrations.RemoveField(model_name="order", name="product"),
        migrations.RemoveField(model_name="order", name="quantity"),
        migrations.RemoveField(model_name="order", name="unit_price"),
    ]
