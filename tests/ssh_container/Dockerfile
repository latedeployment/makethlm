FROM alpine:latest

RUN apk add --no-cache openssh-server \
    && ssh-keygen -A \
    && adduser -D testuser \
    && passwd -u testuser \
    && mkdir -p /home/testuser/.ssh \
    && chmod 700 /home/testuser/.ssh

COPY authorized_keys /home/testuser/.ssh/authorized_keys

RUN chown -R testuser:testuser /home/testuser/.ssh \
    && chmod 600 /home/testuser/.ssh/authorized_keys

# Key-based auth only, no password
RUN sed -i \
    -e 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' \
    -e 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' \
    -e 's/^#*UsePAM.*/UsePAM no/' \
    -e 's/^#*PermitRootLogin.*/PermitRootLogin no/' \
    /etc/ssh/sshd_config

EXPOSE 22

CMD ["/usr/sbin/sshd", "-D", "-e"]
