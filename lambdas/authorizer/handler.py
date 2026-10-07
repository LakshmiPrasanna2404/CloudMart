                "Request had an invalid token",
            )

            return unauthorized()

        role = identity["role"]
        customer_id = identity.get("customer_id")

        # ----------------------------------------------------
        # CHECK ROUTE PERMISSION
        # ----------------------------------------------------

        permission = get_route_permission(
            method,
            path,
        )

        if permission is None:
            log(
                "WARN",
                "No matching route or method",
                method=method,
                path=path,
                role=role,
            )

            return json_response(
                404,
                {
                    "error": "not_found",
                    "message": "No matching route",
                },
            )

        if not role_allows(
            role,
            permission,
            method,
        ):
            log(
                "WARN",
                "RBAC permission denied",
                role=role,
                method=method,
                path=path,
                resource=permission,
            )

            return forbidden()

        # ----------------------------------------------------
        # FIND TARGET LAMBDA
        # ----------------------------------------------------

        target_function = get_target_lambda(path)

        if not target_function:
            return json_response(
                404,
                {
                    "error": "not_found",
                    "message": "Target Lambda not found",
                },
            )

        # ----------------------------------------------------
        # PASS AUTHENTICATED IDENTITY DOWNSTREAM
        # ----------------------------------------------------

        request_context = event.setdefault(
            "requestContext",
            {},
        )

        authorizer_context = request_context.setdefault(
            "authorizer",
            {},
        )

        authorizer_context["role"] = role

        if customer_id is not None:
            authorizer_context["customer_id"] = customer_id

        log(
            "INFO",
            "RBAC authorized, invoking downstream Lambda",
            role=role,
            customer_id=customer_id,
            method=method,
            path=path,
            target=target_function,
        )

        # ----------------------------------------------------
        # INVOKE PRODUCT OR ORDER LAMBDA
        # ----------------------------------------------------

        return invoke_downstream_lambda(
            target_function,
            event,
        )

    except Exception as error:
        log(
            "ERROR",
            "Authorizer execution failed",
            error=str(error),
        )

        return json_response(
            500,
            {
                "error": "internal_error",
                "message": "Authentication or authorization failed",
            },
        )
