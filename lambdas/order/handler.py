# --------------------------------------------------------
# POST /orders/{id}/cancel
# --------------------------------------------------------

elif (
    method == "POST"
    and cancel_match
):

    order_id = int(cancel_match.group(1))

    customer_id = body.get("customer_id")

    if role != "admin":
        if customer_id is None:
            return response(
                400,
                {
                    "error": "validation_error",
                    "message": "customer_id is required"
                }
            )

        try:
            customer_id = int(customer_id)
        except (ValueError, TypeError):
            return response(
                400,
                {
                    "error": "validation_error",
                    "message": "customer_id must be an integer"
                }
            )

    return cancel_order(
        order_id,
        customer_id,
        role
    )
