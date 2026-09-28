package main

# Deny any HTTPS listener that uses an outdated SSL policy
deny[msg] {
    resource := input.resource.aws_lb_listener[name]
    resource.protocol == "HTTPS"
    
    # List of unapproved/outdated security policies
    outdated_policies := ["ELBSecurityPolicy-2016-08", "ELBSecurityPolicy-TLS-1-0-2015-04"]
    resource.ssl_policy == outdated_policies[_]

    msg := sprintf("CRITICAL: aws_lb_listener.%v uses an insecure, outdated TLS policy: '%v'. Upgrade to 'ELBSecurityPolicy-TLS13-1-2-2021-06'.", [name, resource.ssl_policy])
}
