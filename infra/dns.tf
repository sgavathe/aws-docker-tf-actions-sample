# Looks up your existing spatialenable.com hosted zone by name - no need to
# hardcode the zone ID.
data "aws_route53_zone" "spatialenable" {
  name         = "spatialenable.com."
  private_zone = false
}

# Certificate for the subdomain, validated via DNS (auto-creates the
# validation CNAME in the same hosted zone).
resource "aws_acm_certificate" "map" {
  domain_name       = "map.spatialenable.com"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "map_cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.map.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id = data.aws_route53_zone.spatialenable.zone_id
  name    = each.value.name
  type    = each.value.type
  ttl     = 60
  records = [each.value.record]
}

resource "aws_acm_certificate_validation" "map" {
  certificate_arn         = aws_acm_certificate.map.arn
  validation_record_fqdns = [for r in aws_route53_record.map_cert_validation : r.fqdn]
}

# HTTPS listener on the ALB using the validated cert - default action
# matches the HTTP listener (forward to frontend).
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-2016-08"
  certificate_arn   = aws_acm_certificate_validation.map.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}

resource "aws_lb_listener_rule" "backend_https" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }

  condition {
    path_pattern {
      values = ["/api/*", "/health"]
    }
  }
}

# Alias record pointing map.spatialenable.com at this ALB. Since this is a
# resource (not a manual DNS entry), it re-points automatically to whatever
# the current ALB is every time you terraform apply - even after a
# destroy/recreate cycle changes the ALB's DNS name.
resource "aws_route53_record" "map" {
  zone_id = data.aws_route53_zone.spatialenable.zone_id
  name    = "map.spatialenable.com"
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
