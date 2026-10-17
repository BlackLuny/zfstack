00000000003c1390 <tokio::net::tcp::stream::TcpStream::poll_read_ready>:
  3c1390:	sub    $0x18,%rsp
  3c1394:	mov    %rsi,%rdx
  3c1397:	mov    %rdi,%rsi
  3c139a:	lea    0x8(%rsp),%rdi
  3c139f:	xor    %ecx,%ecx
  3c13a1:	call   3bf530 <tokio::runtime::io::registration::Registration::poll_ready>
  3c13a6:	movzbl 0x11(%rsp),%eax
  3c13ab:	cmp    $0x2,%eax
  3c13ae:	je     3c13bf <tokio::net::tcp::stream::TcpStream::poll_read_ready+0x2f>
  3c13b0:	cmp    $0x3,%eax
  3c13b3:	jne    3c13cb <tokio::net::tcp::stream::TcpStream::poll_read_ready+0x3b>
  3c13b5:	mov    $0x1,%eax
  3c13ba:	add    $0x18,%rsp
  3c13be:	ret
  3c13bf:	mov    0x8(%rsp),%rdx
  3c13c4:	xor    %eax,%eax
  3c13c6:	add    $0x18,%rsp
  3c13ca:	ret
  3c13cb:	xor    %edx,%edx
  3c13cd:	xor    %eax,%eax
  3c13cf:	add    $0x18,%rsp
  3c13d3:	ret
  3c13d4:	cs nopw 0x0(%rax,%rax,1)
  3c13de:	xchg   %ax,%ax

