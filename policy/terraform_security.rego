package main

import rego.v1

outdated := {"ELBSecurityPolicy-2016-08", "ELBSecurityPolicy-TLS-1-0-2015-04"}

deny contains msg if {
    rc := input.resource_changes[_]
    rc.type == "aws_lb_listener"
    after := rc.change.after
    after.protocol == "HTTPS"
    after.ssl_policy in outdated
    msg := sprintf("%v uses outdated TLS policy '%v'; use ELBSecurityPolicy-TLS13-1-2-2021-06", [rc.address, after.ssl_policy])
}